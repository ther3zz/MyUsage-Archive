"""The --grids-only path must be lean: one login, two grid fetches, no extras.

Scheduled repeat captures (the Nov 2026 DST window) run daily for a week, so
the request count per run is a real politeness constraint against an
undocumented vendor endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import make_interval_grid
from test_client import LANDING_URL, REDIRECT_URL, FakeResponse, FakeSession

import myusage_archive.probe as probe_mod
from myusage_archive.probe import ProbeRunner

DATES = ["09/06", "09/05"]
WEEKDAYS = ["Sun", "Sat"]
GRID15 = make_interval_grid(
    "grid15MinuteUsage", DATES, WEEKDAYS,
    ["°F", "kWh Del", "kWh Rcvd", "kW"], ["12:00 AM"],
    {"09/06": [["76", "0.4", "0.0", "1.6"]], "09/05": [["77", "0.5", "0.0", "2.0"]]},
)
GRIDH = make_interval_grid(
    "gridHourlyUsage", DATES, WEEKDAYS,
    ["°F", "kWh Del", "kWh Rcvd"], ["12:00 AM - 01:00 AM"],
    {"09/06": [["76", "1.0", "0.0"]], "09/05": [["77", "1.4", "0.0"]]},
)


def _session() -> FakeSession:
    s = FakeSession()
    s.add("GET", "https://www.myusage.com/",
          FakeResponse("<html>home</html>", "https://www.myusage.com/"))
    s.add("POST", "https://www.myusage.com/login",
          FakeResponse(json.dumps({"data": "ok", "redirect_url": REDIRECT_URL}),
                       "https://www.myusage.com/login"))
    s.add("GET", REDIRECT_URL[:60], FakeResponse("<html>app</html>", LANDING_URL))
    s.add("GET", "https://www.myusage.com/data.cfm?appPage=Postpaid&appPageScreen=History"
          "&appPageScreenSub=Usage%20History&appFlow=2026090715343480"
          "&appTransition=View+15+Minute+Usage",
          FakeResponse(GRID15, "https://www.myusage.com/data.cfm"))
    s.add("GET", "https://www.myusage.com/data.cfm?appPage=Postpaid&appPageScreen=History"
          "&appPageScreenSub=Usage%20History&appFlow=2026090715343480"
          "&appTransition=View+Hourly+Usage",
          FakeResponse(GRIDH, "https://www.myusage.com/data.cfm"))
    s.routes.sort(key=lambda r: -len(r[1]))
    return s


async def test_grids_only_makes_minimal_requests(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(probe_mod, "_pause", lambda: __import__("asyncio").sleep(0))
    session = _session()
    monkeypatch.setattr(
        probe_mod.aiohttp, "ClientSession", lambda **kw: session  # type: ignore[arg-type]
    )

    runner = ProbeRunner("u@example.com", "pw", tmp_path, grids_only=True)
    report_path = await runner.run()

    methods = [(m, u) for m, u, _ in session.requests]
    # One login (prime GET + POST + SSO follow) then exactly the two grids.
    assert len(methods) == 5, methods
    assert sum(1 for m, _ in methods if m == "POST") == 1
    assert sum(1 for _, u in methods if "View+15+Minute+Usage" in u) == 1
    assert sum(1 for _, u in methods if "View+Hourly+Usage" in u) == 1
    # No history page, no date-range POSTs, no settings pages.
    assert not any("appTransition" not in u and "data.cfm" in u for _, u in methods)

    report = json.loads(report_path.read_text())
    assert set(report["captures"]) == {"03-grid15.html", "04-gridhourly.html"}
    assert "grid15MinuteUsage" in report["layout_signatures"]
    assert report["leak_scan"] == []
    # The auth matrix must not have run its variant logins.
    assert len(report["auth_matrix"]) == 1


async def test_grids_only_records_layout_signature(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(probe_mod, "_pause", lambda: __import__("asyncio").sleep(0))
    session = _session()
    monkeypatch.setattr(
        probe_mod.aiohttp, "ClientSession", lambda **kw: session  # type: ignore[arg-type]
    )
    runner = ProbeRunner("u@example.com", "pw", tmp_path, grids_only=True)
    await runner.run()
    sig = json.loads((tmp_path / "probe-report.json").read_text())["layout_signatures"]
    assert sig["grid15MinuteUsage"]["stride"] == 4
    assert sig["grid15MinuteUsage"]["metrics"] == [
        "temp_f", "kwh_delivered", "kwh_received", "kw",
    ]
    assert sig["gridHourlyUsage"]["stride"] == 3
