"""Pipeline tests: portal → parser → archive against a fake session.

The most important assertion here is architectural: the archive is created
WITHOUT the event-loop escape hatch, so if the pipeline ever touched SQLite
from the loop, BlockingCallError would fail the test. That is the discipline
Home Assistant needs and cannot enforce for us.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from conftest import make_interval_grid
from test_client import LANDING_URL, REDIRECT_URL, FakeResponse, FakeSession

from myusage_archive.archive import Archive
from myusage_archive.client import MyUsageClient
from myusage_archive.exceptions import LayoutError, UnsupportedAccountError
from myusage_archive.service import KIND_DAILY, KIND_INTERVALS, Pipeline, reparse

FIXTURES = Path(__file__).parent / "fixtures" / "live"
REF = dt.date(2026, 9, 8)
HISTORY_URL = (
    "https://www.myusage.com/data.cfm?appPage=Postpaid&appPageScreen=History"
    "&appPageScreenSub=Usage%20History&appFlow=2026090715343480"
)


def _session(grid15_html: str, daily_html: str) -> FakeSession:
    s = FakeSession()
    s.add("GET", "https://www.myusage.com/", FakeResponse("<html>home</html>", "https://www.myusage.com/"))
    s.add("POST", "https://www.myusage.com/login",
          FakeResponse(json.dumps({"data": "ok", "redirect_url": REDIRECT_URL}),
                       "https://www.myusage.com/login"))
    s.add("GET", REDIRECT_URL[:60], FakeResponse("<html>app</html>", LANDING_URL))
    s.add("GET", HISTORY_URL, FakeResponse(daily_html, HISTORY_URL))
    s.add("GET", HISTORY_URL + "&appTransition=View+15+Minute+Usage",
          FakeResponse(grid15_html, HISTORY_URL))
    s.routes.sort(key=lambda r: -len(r[1]))
    return s


def _live_pages() -> tuple[str, str]:
    return (
        (FIXTURES / "03-grid15.html").read_text(encoding="utf-8"),
        (FIXTURES / "06-post-electric-60d.html").read_text(encoding="utf-8"),
    )


async def test_cycle_stores_intervals_and_daily_without_touching_loop(tmp_path: Path) -> None:
    grid15, daily = _live_pages()
    session = _session(grid15, daily)
    # No allow_event_loop: any on-loop archive access would raise BlockingCallError.
    archive = Archive(tmp_path / "a.db", keep_ok_raw_pages=2)
    pipeline = Pipeline(
        MyUsageClient("u@example.com", "pw", session),  # type: ignore[arg-type]
        archive,
        reference_date=REF,
    )
    result = await pipeline.run_cycle()

    assert result.meter == "MTR001"
    assert result.intervals.ok and result.intervals.store is not None
    assert result.intervals.store.inserted == 672
    assert result.daily is not None and result.daily.store is not None
    assert result.daily.store.inserted == 59
    assert result.gaps is not None and result.gaps.permanent_missing == 0
    assert result.intervals.signature is not None
    assert result.intervals.signature.stride == 4

    # Verify from a worker thread, as HA would.
    import asyncio

    stats = await asyncio.to_thread(archive.stats)
    assert stats["rows"]["interval_readings"] == 672
    assert stats["rows"]["daily_reads"] == 59
    assert stats["rows"]["fetches"] == 2
    assert stats["rows"]["raw_pages"] == 2  # kept because keep_ok_raw_pages=2


async def test_second_cycle_is_a_no_op(tmp_path: Path) -> None:
    grid15, daily = _live_pages()
    archive = Archive(tmp_path / "a.db")
    for _ in range(2):
        pipeline = Pipeline(
            MyUsageClient("u@example.com", "pw", _session(grid15, daily)),  # type: ignore[arg-type]
            archive, reference_date=REF,
        )
        result = await pipeline.run_cycle()
    assert result.intervals.store is not None
    assert (result.intervals.store.inserted, result.intervals.store.unchanged) == (0, 672)
    assert result.intervals.store.revisions == 0


async def test_layout_failure_keeps_raw_page_and_raises(tmp_path: Path) -> None:
    """A page that fetches but does not parse must be kept for forensics."""
    _, daily = _live_pages()
    broken = make_interval_grid(
        "grid15MinuteUsage", ["09/06"], ["Sun"], ["°F", "Flux", "kWh Rcvd", "kW"],
        ["12:00 AM"], {"09/06": [["70", "1", "0", "4"]]},
    )
    archive = Archive(tmp_path / "a.db")
    pipeline = Pipeline(
        MyUsageClient("u@example.com", "pw", _session(broken, daily)),  # type: ignore[arg-type]
        archive, reference_date=REF,
    )
    with pytest.raises(LayoutError, match="unrecognized metric header"):
        await pipeline.run_cycle()

    import asyncio

    history = await asyncio.to_thread(archive.fetch_history)
    failed = [f for f in history if not f["ok"]]
    assert len(failed) == 1
    assert failed[0]["kind"] == KIND_INTERVALS
    assert "Flux" in failed[0]["error"]
    pages = await asyncio.to_thread(archive.raw_pages, ok=False)
    assert len(pages) == 1 and pages[0].has_content
    content = await asyncio.to_thread(archive.raw_page_content, pages[0].id)
    assert content is not None and "Flux" in content


async def test_multi_meter_daily_table_is_refused_loudly(tmp_path: Path) -> None:
    grid15, daily = _live_pages()
    two_meters = re.sub(r"(>\s*)MTR001(\s*<)", r"\1MTR002\2", daily, count=3)
    assert "MTR002" in two_meters
    archive = Archive(tmp_path / "a.db")
    pipeline = Pipeline(
        MyUsageClient("u@example.com", "pw", _session(grid15, two_meters)),  # type: ignore[arg-type]
        archive, reference_date=REF,
    )
    with pytest.raises(UnsupportedAccountError, match="exactly one meter"):
        await pipeline.run_cycle()


async def test_explicit_meter_overrides_discovery(tmp_path: Path) -> None:
    grid15, daily = _live_pages()
    archive = Archive(tmp_path / "a.db")
    pipeline = Pipeline(
        MyUsageClient("u@example.com", "pw", _session(grid15, daily)),  # type: ignore[arg-type]
        archive, reference_date=REF,
    )
    result = await pipeline.run_cycle(meter="MTR001")
    assert result.meter == "MTR001"
    with pytest.raises(UnsupportedAccountError, match="not present"):
        await Pipeline(
            MyUsageClient("u@example.com", "pw", _session(grid15, daily)),  # type: ignore[arg-type]
            archive, reference_date=REF,
        ).run_cycle(meter="NOPE")


def test_reparse_reingests_retained_pages(tmp_path: Path) -> None:
    """After a parser fix, retained pages are re-run through the normal upsert."""
    grid15, daily = _live_pages()
    archive = Archive(tmp_path / "a.db", allow_event_loop=True, keep_ok_raw_pages=5)
    # Simulate an earlier run that kept pages but (say) stored nothing useful.
    fid_grid = archive.record_fetch(
        KIND_INTERVALS, ok=False, error="old parser failed", raw_html=grid15,
        fetched_at_utc=int(dt.datetime(2026, 9, 8, 13, 0, tzinfo=dt.UTC).timestamp()),
    )
    fid_daily = archive.record_fetch(KIND_DAILY, ok=True, raw_html=daily)
    assert archive.stats()["rows"]["interval_readings"] == 0

    outcomes = reparse(archive, meter="MTR001")
    by_kind = {o.kind: o for o in outcomes}
    assert by_kind[KIND_INTERVALS].fetch_id == fid_grid
    assert by_kind[KIND_INTERVALS].store is not None
    assert by_kind[KIND_INTERVALS].store.inserted == 672
    assert by_kind[KIND_DAILY].fetch_id == fid_daily
    assert by_kind[KIND_DAILY].store is not None
    assert by_kind[KIND_DAILY].store.inserted == 59

    # Second reparse: everything unchanged, no revisions.
    again = {o.kind: o for o in reparse(archive, meter="MTR001")}
    assert again[KIND_INTERVALS].store is not None
    assert again[KIND_INTERVALS].store.unchanged == 672
    assert archive.revisions_since(0) == []


def test_reparse_reports_pages_that_still_fail(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "a.db", allow_event_loop=True)
    archive.record_fetch(KIND_INTERVALS, ok=False, error="x", raw_html="<html>nothing</html>")
    outcomes = reparse(archive, meter="MTR001")
    assert len(outcomes) == 1
    assert outcomes[0].store is None
    assert outcomes[0].error and "not found" in outcomes[0].error
