"""M0 probe harness: answer the portal-behavior questions from one live run.

The operator runs this with their own credentials (see cli.py). It performs a
short, polite request sequence (~12 requests, 3–6 s apart), saves every raw
page locally (git-ignored), writes an anonymized fixture bundle plus a
``probe-report.json`` answering the P-probes from the implementation plan:

  P1  does kWh Received go non-zero midday?
  P2  what are the two trailing rows in each interval grid?
  P4  daily-history default window / Electric range-POST / max span
  P5  services + meters visible on this account
  P7  auth matrix (which login element is load-bearing) + next-day recheck
  P8  MFA-ish strings on settings pages
  P10 15-min label = interval START or END?
  P11 landing-page shape

All analysis here is deliberately best-effort and self-contained: it reads the
grids heuristically to produce evidence, and records anomalies instead of
crashing. The strict production parser is milestone M1 and shares no code
with this module.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypedDict

import aiohttp
from bs4 import BeautifulSoup
from bs4.element import Tag

from . import __version__
from .anonymize import Anonymizer
from .client import LoginAttempt, LoginMode, MyUsageClient  # noqa: F401 - LoginAttempt typing
from .const import GRID_15MIN_ID, GRID_DAILY_ID, GRID_HOURLY_ID, STUB_SIZE_HINT
from .exceptions import MyUsageError
from .redact import scrub_text

_LOGGER = logging.getLogger(__name__)

_DELAY_RANGE_S = (3.0, 6.0)


class Cell(TypedDict):
    """One grid cell: rendered text plus the optional data-raw-value."""

    text: str
    raw: str | None


async def _pause() -> None:
    await asyncio.sleep(random.uniform(*_DELAY_RANGE_S))  # noqa: S311 - politeness jitter


# --------------------------------------------------------------------------- grids


def grid_to_matrix(html: str, table_id: str) -> list[list[Cell]] | None:
    """Extract a grid table into a matrix of {text, raw} cells; None if absent."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=table_id)
    if not isinstance(table, Tag):
        return None
    matrix: list[list[Cell]] = []
    for row in table.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        cells: list[Cell] = []
        for cell in row.find_all(("th", "td")):
            if not isinstance(cell, Tag):
                continue
            raw = cell.get("data-raw-value")
            cells.append(
                {
                    "text": " ".join(cell.get_text(" ", strip=True).split()),
                    "raw": raw if isinstance(raw, str) else None,
                }
            )
        if cells:
            matrix.append(cells)
    return matrix


def _cell_decimal(cell: Cell) -> Decimal | None:
    for source in (cell.get("raw"), cell.get("text")):
        if source is None:
            continue
        cleaned = source.replace(",", "").replace("°", "").strip()
        if cleaned in ("", "-", "—"):
            continue
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            continue
    return None


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{2})/(\d{2})$")


def _parse_label_minutes(label: str) -> int | None:
    """Minutes since midnight for a '12:00 AM' / '12:00 AM - 01:00 AM' label."""
    match = _TIME_RE.match(label.strip())
    if not match:
        return None
    hour, minute, ampm = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if hour == 12:
        hour = 0
    if ampm == "PM":
        hour += 12
    return hour * 60 + minute


def _canonical_metric(text: str) -> str:
    lowered = text.lower()
    if "°" in text or "temp" in lowered or lowered == "f":
        return "temp_f"
    compact = re.sub(r"[^a-z]", "", lowered)
    if compact.startswith("kwh"):
        rest = compact[3:]
        # Live grids abbreviate: "kWh Del" / "kWh Rcvd" (observed 2026-09-08).
        if rest.startswith("del"):
            return "kwh_delivered"
        if rest.startswith(("rec", "rcvd")):
            return "kwh_received"
        if not rest:
            return "kwh"
    if "deliver" in lowered:
        return "kwh_delivered"
    if "receiv" in lowered or "rcvd" in lowered:
        return "kwh_received"
    if compact == "kw":
        return "kw"
    return f"unknown:{text}"


@dataclass
class GridView:
    """Best-effort structural read of one interval grid."""

    table_id: str
    dates: list[str]
    weekdays: list[str]
    stride: int
    metrics: list[str]  # canonical names, one group's ordered tuple
    header_rows: int
    data_rows: list[list[Cell]]
    trailing_rows: list[list[Cell]]
    anomalies: list[str]

    def column(self, day_index: int, metric: str) -> int | None:
        if metric not in self.metrics:
            return None
        return 1 + day_index * self.stride + self.metrics.index(metric)


def read_grid(matrix: list[list[Cell]], table_id: str) -> GridView:
    """Interpret the 4-row header block + shared-row body of an interval grid."""
    anomalies: list[str] = []

    # Locate the metric-header row: first cell says Time or Hour.
    metric_row_idx = None
    for idx, row in enumerate(matrix[:6]):
        first = row[0]["text"].lower() if row else ""
        if first.startswith(("time", "hour")):
            metric_row_idx = idx
            break
    if metric_row_idx is None:
        anomalies.append("no Time/Hour header row found in the first 6 rows; assuming index 3")
        metric_row_idx = 3

    date_row = matrix[0] if matrix else []
    weekday_row = matrix[1] if len(matrix) > 1 else []
    dates = [c["text"] for c in date_row[1:] if _DATE_RE.match(c["text"])]
    weekdays = [c["text"] for c in weekday_row[1:] if c["text"]][: len(dates)]
    if not dates:
        anomalies.append("no MM/DD headers found in row 0")

    metric_cells = [c["text"] for c in matrix[metric_row_idx][1:]]
    day_count = len(dates) or 7
    stride, metrics = 0, []
    if metric_cells and day_count and len(metric_cells) % day_count == 0:
        stride = len(metric_cells) // day_count
        groups = [
            tuple(_canonical_metric(t) for t in metric_cells[i * stride : (i + 1) * stride])
            for i in range(day_count)
        ]
        if len(set(groups)) == 1:
            metrics = list(groups[0])
        else:
            anomalies.append(f"per-day metric groups differ: {sorted(set(groups))!r}")
            metrics = list(groups[0])
    else:
        anomalies.append(
            f"metric cell count {len(metric_cells)} not divisible by day count {day_count}"
        )

    body = matrix[metric_row_idx + 1 :]
    data_rows = [r for r in body if r and _parse_label_minutes(r[0]["text"]) is not None]
    trailing_rows = [r for r in body if not r or _parse_label_minutes(r[0]["text"]) is None]

    return GridView(
        table_id=table_id,
        dates=dates,
        weekdays=weekdays,
        stride=stride,
        metrics=metrics,
        header_rows=metric_row_idx + 1,
        data_rows=data_rows,
        trailing_rows=trailing_rows,
        anomalies=anomalies,
    )


_LEAK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("hex32_token", re.compile(r"\b[0-9a-fA-F]{32}\b")),
    ("session_cookie", re.compile(r"CF(?:ID|TOKEN)=[^&\";\s]+", re.IGNORECASE)),
    ("account_label", re.compile(r"account\s*(?:#|number)[^0-9]{0,40}(\d{4,})", re.IGNORECASE)),
    ("id_param", re.compile(r"(?:meter|account|premise|customer)id=(\d{4,})", re.IGNORECASE)),
]

# Values that are legitimately numeric and not identifiers.
_LEAK_ALLOW = re.compile(r"^(?:19|20)\d{2}(?:\d{2}){0,6}$")  # dates / appFlow timestamps
_TAG_RE = re.compile(r"<[^>]{0,300}>")


def scan_for_leaks(files: dict[str, str], email: str | None = None) -> list[dict[str, Any]]:
    """Look for identifiers that survived anonymization.

    Runs over the *anonymized* output so a hit means something escaped. Returns
    one entry per (file, kind, value) with a short context excerpt.
    """
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for name, text in files.items():
        # Scan the raw text AND a tag-stripped view: markup between a label and
        # its value ("<h3>Account #</h3><h2>1234</h2>") contains digits in the
        # tag names themselves, which would otherwise hide the value.
        views = [text, _TAG_RE.sub(" ", text)]
        for view in views:
            for kind, pattern in _LEAK_PATTERNS:
                for match in pattern.finditer(view):
                    value = match.group(1) if match.groups() else match.group(0)
                    if _LEAK_ALLOW.match(value):
                        continue
                    if value.lower() in {"user@example.com"}:
                        continue
                    key = (name, kind, value)
                    if key in seen:
                        continue
                    seen.add(key)
                    start = max(0, match.start() - 40)
                    findings.append(
                        {
                            "file": name,
                            "kind": kind,
                            "value": value,
                            "context": " ".join(
                                view[start : match.end() + 20].split()
                            ),
                        }
                    )
    if email:
        for name, text in files.items():
            local = email.split("@", 1)[0]
            if len(local) >= 3 and local.lower() in text.lower():
                findings.append(
                    {"file": name, "kind": "email_local_part", "value": local, "context": ""}
                )
    return findings


def classify_page(html: str) -> dict[str, Any]:
    """Cheap page classification for the report."""
    soup = BeautifulSoup(html, "html.parser")
    tables = [
        str(t.get("id"))
        for t in soup.find_all("table")
        if isinstance(t, Tag) and t.get("id") is not None
    ]
    grid_tables = [t for t in tables if t.lower().startswith("grid")]
    size = len(html.encode())
    return {
        "bytes": size,
        "grid_tables": grid_tables,
        "looks_like_stub": not grid_tables and abs(size - STUB_SIZE_HINT) < 1500,
    }


# ------------------------------------------------------------------ probe analyses


def analyze_p1_received_midday(view: GridView) -> dict[str, Any]:
    """P1: kWh Received values in the 10:00–16:00 local window, per day column."""
    metric = "kwh_received" if "kwh_received" in view.metrics else None
    if metric is None:
        return {"answer": "no kWh Received metric in this grid", "per_day": []}
    per_day = []
    for day_i, date in enumerate(view.dates):
        col = view.column(day_i, metric)
        values: list[Decimal] = []
        assert col is not None
        for row in view.data_rows:
            minutes = _parse_label_minutes(row[0]["text"])
            if minutes is None or not (10 * 60 <= minutes < 16 * 60):
                continue
            if col < len(row):
                value = _cell_decimal(row[col])
                if value is not None:
                    values.append(value)
        per_day.append(
            {
                "date": date,
                "samples": len(values),
                "nonzero": sum(1 for v in values if v != 0),
                "max": str(max(values)) if values else None,
                "sum": str(sum(values)) if values else None,
            }
        )
    populated = [d for d in per_day if d["samples"]]
    any_nonzero = any(d["nonzero"] for d in populated)
    return {
        "answer": (
            "YES - kWh Received is non-zero midday"
            if any_nonzero
            else "NO non-zero midday kWh Received observed in this capture"
            if populated
            else "no midday samples found (unexpected)"
        ),
        "per_day": per_day,
    }


def analyze_p2_trailing_rows(view: GridView) -> dict[str, Any]:
    """P2: labels and sample cells of the non-time trailing rows."""
    rows = []
    for row in view.trailing_rows:
        rows.append(
            {
                "label": row[0]["text"] if row else "",
                "cells": [c["text"] for c in row[1:9]],
                "cell_count": len(row),
            }
        )
    return {"count": len(rows), "rows": rows}


def analyze_p10_alignment(view15: GridView, view_hourly: GridView) -> dict[str, Any]:
    """P10: does the hourly 12–1 AM bucket equal the sum of 15-min rows
    labeled 12:00–12:45 (labels are STARTS) or 12:15–01:00 (labels are ENDS)?"""
    metric15 = "kwh_delivered" if "kwh_delivered" in view15.metrics else "kwh"
    metric_h = "kwh_delivered" if "kwh_delivered" in view_hourly.metrics else "kwh"
    start_labels = {0, 15, 30, 45}
    end_labels = {15, 30, 45, 60}
    per_day = []
    for day_i, date in enumerate(view15.dates):
        if date not in view_hourly.dates:
            continue
        h_day = view_hourly.dates.index(date)
        col_h = view_hourly.column(h_day, metric_h)
        col_15 = view15.column(day_i, metric15)
        if col_h is None or col_15 is None:
            continue
        hourly_value: Decimal | None = None
        for row in view_hourly.data_rows:
            if _parse_label_minutes(row[0]["text"]) == 0 and col_h < len(row):
                hourly_value = _cell_decimal(row[col_h])
                break
        sums = {"start": Decimal(0), "end": Decimal(0)}
        counts = {"start": 0, "end": 0}
        for row in view15.data_rows:
            minutes = _parse_label_minutes(row[0]["text"])
            if minutes is None or col_15 >= len(row):
                continue
            value = _cell_decimal(row[col_15])
            if value is None:
                continue
            if minutes in start_labels:
                sums["start"] += value
                counts["start"] += 1
            if minutes in end_labels:
                sums["end"] += value
                counts["end"] += 1
        record: dict[str, Any] = {
            "date": date,
            "hourly_12_to_1": str(hourly_value) if hourly_value is not None else None,
            "sum_if_labels_are_starts": str(sums["start"]) if counts["start"] == 4 else None,
            "sum_if_labels_are_ends": str(sums["end"]) if counts["end"] == 4 else None,
        }
        if hourly_value is not None:
            tolerance = Decimal("0.002")
            record["start_matches"] = (
                counts["start"] == 4 and abs(sums["start"] - hourly_value) <= tolerance
            )
            record["end_matches"] = (
                counts["end"] == 4 and abs(sums["end"] - hourly_value) <= tolerance
            )
        per_day.append(record)
    verdicts = {
        "start": sum(1 for d in per_day if d.get("start_matches")),
        "end": sum(1 for d in per_day if d.get("end_matches")),
        "days_compared": len(per_day),
    }
    if verdicts["start"] and not verdicts["end"]:
        answer = "labels are interval STARTS"
    elif verdicts["end"] and not verdicts["start"]:
        answer = "labels are interval ENDS"
    elif verdicts["start"] and verdicts["end"]:
        answer = "ambiguous (both hypotheses matched on some days)"
    else:
        answer = "inconclusive (no day matched either hypothesis)"
    return {"answer": answer, "verdicts": verdicts, "per_day": per_day}


def _daily_table_dates(html: str) -> dict[str, Any]:
    """Row count and From/To date span of gridUsageHistory, header-driven."""
    matrix = grid_to_matrix(html, GRID_DAILY_ID)
    if matrix is None:
        return {"present": False, **classify_page(html)}
    header = [c["text"].lower() for c in matrix[0]]

    def col_of(*needles: str) -> int | None:
        for i, text in enumerate(header):
            if any(n in text for n in needles):
                return i
        return None

    from_col, to_col = col_of("from"), col_of("to")
    dates: list[str] = []
    for row in matrix[1:]:
        for col in (from_col, to_col):
            if col is not None and col < len(row):
                match = re.search(r"\d{2}/\d{2}/\d{4}", row[col]["text"])
                if match:
                    dates.append(match.group(0))

    def key(d: str) -> dt.date:
        return dt.datetime.strptime(d, "%m/%d/%Y").date()  # noqa: DTZ007

    return {
        "present": True,
        "header": [c["text"] for c in matrix[0]],
        "data_rows": len(matrix) - 1,
        "earliest": min(dates, key=key) if dates else None,
        "latest": max(dates, key=key) if dates else None,
    }


def analyze_p5_services_meters(history_html: str) -> dict[str, Any]:
    """P5/P3 evidence: meters in the daily table + ServiceType options."""
    soup = BeautifulSoup(history_html, "html.parser")
    options: list[str] = []
    for select in soup.find_all("select"):
        if isinstance(select, Tag) and str(select.get("name") or "") == "ServiceType":
            options = [
                o.get_text(strip=True) for o in select.find_all("option") if isinstance(o, Tag)
            ]
    meters: list[str] = []
    matrix = grid_to_matrix(history_html, GRID_DAILY_ID)
    if matrix:
        header = [c["text"].lower() for c in matrix[0]]
        meter_col = next((i for i, t in enumerate(header) if "meter" in t), 0)
        for row in matrix[1:]:
            if meter_col < len(row):
                text = row[meter_col]["text"]
                if text and text not in meters:
                    meters.append(text)
    return {"service_options": options, "meters_seen": meters}


_MFA_RE = re.compile(
    r"(multi.?factor|two.?factor|\b2fa\b|authenticator|one.?time\s+(code|passcode)|\botp\b)",
    re.IGNORECASE,
)


def analyze_p8_mfa(pages: dict[str, str]) -> dict[str, Any]:
    """P8: MFA-ish strings on captured settings pages, with a little context."""
    hits = []
    for name, html in pages.items():
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        for match in _MFA_RE.finditer(text):
            start = max(0, match.start() - 60)
            hits.append({"page": name, "context": text[start : match.end() + 60]})
    return {"hits": hits, "answer": "MFA-related strings found" if hits else "none found"}


def layout_signature(view: GridView, row_total: int) -> dict[str, Any]:
    return {
        "table_id": view.table_id,
        "dates": view.dates,
        "weekdays": view.weekdays,
        "day_count": len(view.dates),
        "stride": view.stride,
        "metrics": view.metrics,
        "header_rows": view.header_rows,
        "data_rows": len(view.data_rows),
        "trailing_rows": len(view.trailing_rows),
        "total_rows": row_total,
        "anomalies": view.anomalies,
    }


# ----------------------------------------------------------------------- the runner


def _discover_settings_links(html: str) -> list[str]:
    """In-app links whose text smells like settings/profile/alerts (max 2)."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    for a in soup.find_all("a"):
        if not isinstance(a, Tag):
            continue
        href = str(a.get("href") or "")
        text = a.get_text(" ", strip=True).lower()
        if "data.cfm" not in href and not href.startswith("?"):
            continue
        if re.search(r"settin|profile|security|alert|account", text):
            found.append(href)
        if len(found) >= 2:
            break
    return found


class ProbeRunner:
    """Runs the full M0 sequence and writes raw + anonymized bundles."""

    def __init__(
        self,
        email: str,
        password: str,
        out_dir: Path,
        scrub_extra: list[str] | None = None,
        skip_auth_matrix: bool = False,
        grids_only: bool = False,
    ) -> None:
        self.email = email
        self.password = password
        self.out = out_dir
        self.raw_dir = out_dir / "raw"
        self.anon_dir = out_dir / "anonymized"
        self.scrub_extra = scrub_extra or []
        # grids_only implies no auth matrix: repeat captures should cost one login.
        self.skip_auth_matrix = skip_auth_matrix or grids_only
        self.grids_only = grids_only
        self.report: dict[str, Any] = {
            "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "tool_version": __version__,
            "notes": [],
        }
        self.pages: dict[str, str] = {}

    # -- bookkeeping

    def _ensure_raw_dir(self) -> None:
        """Create the raw dir (0700) with a self-ignoring .gitignore.

        The raw captures contain real account data; the in-directory ignore
        file keeps them out of git even when --out points somewhere the
        repo-level .gitignore does not cover.
        """
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.chmod(0o700)
        marker = self.raw_dir / ".gitignore"
        if not marker.exists():
            marker.write_text("*\n", encoding="utf-8")

    def _save_raw(self, name: str, content: str) -> None:
        self._ensure_raw_dir()
        path = self.raw_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        self.pages[name] = content

    def _note(self, message: str) -> None:
        self.report["notes"].append(scrub_text(message, [self.password, self.email]))
        _LOGGER.info("%s", scrub_text(message, [self.password, self.email]))

    async def _capture(self, name: str, coro: Any) -> str | None:
        """Await a fetch, save it, classify it; never let one failure end the run."""
        await _pause()
        try:
            html: str = await coro
        except MyUsageError as err:
            self._note(f"{name}: FAILED - {err}")
            self.report.setdefault("captures", {})[name] = {"error": str(err)}
            return None
        self._save_raw(name, html)
        self.report.setdefault("captures", {})[name] = classify_page(html)
        self._note(f"{name}: captured {len(html)} bytes")
        return html

    # -- phases

    # Successive logins may be throttled by the portal (the 2026-09-08 run saw
    # a canonical login fail minutes after a successful login-test), so login
    # attempts are spaced generously and the matrix variants run AFTER the
    # captures instead of back-to-back with the canonical login.
    _LOGIN_GAP_S = 45.0
    _RETRY_AFTER_S = 60.0

    async def _attempt(
        self, mode: LoginMode, label: str
    ) -> tuple[LoginAttempt, MyUsageClient, aiohttp.ClientSession]:
        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        client = MyUsageClient(self.email, self.password, session)
        attempt = await client.attempt_login(mode)
        attempt.mode = label
        self._note(f"auth[{label}]: ok={attempt.ok} error={attempt.error}")
        return attempt, client, session

    async def _login_canonical(self) -> MyUsageClient | None:
        """Canonical login for the capture session, with one spaced retry."""
        results = self.report.setdefault("auth_matrix", [])
        attempt, client, session = await self._attempt(
            LoginMode.CANONICAL, LoginMode.CANONICAL.value
        )
        results.append(asdict(attempt))
        if not attempt.ok:
            await session.close()
            self._note(f"canonical login failed; retrying once in {self._RETRY_AFTER_S:.0f}s")
            await asyncio.sleep(self._RETRY_AFTER_S)
            attempt, client, session = await self._attempt(
                LoginMode.CANONICAL, "prime+xhr (retry)"
            )
            results.append(asdict(attempt))
        if not attempt.ok:
            await session.close()
            self._note("canonical login failed twice; skipping captures and variants")
            self._keeper_session = None
            return None
        self._keeper_session = session
        return client

    async def _auth_variants(self) -> None:
        """The auth matrix variants, spaced out after the captures."""
        results = self.report.setdefault("auth_matrix", [])
        for mode in (LoginMode.PRIME_ONLY, LoginMode.XHR_ONLY):
            await asyncio.sleep(self._LOGIN_GAP_S)
            attempt, _client, session = await self._attempt(mode, mode.value)
            results.append(asdict(attempt))
            await session.close()

    async def _persist_session(self, session: aiohttp.ClientSession, client: MyUsageClient) -> None:
        """Save cookies + appFlow so `probe --recheck` can test session lifetime."""
        self._ensure_raw_dir()
        jar = session.cookie_jar
        cookie_path = self.raw_dir / "session.cookies"
        if isinstance(jar, aiohttp.CookieJar):
            jar.save(cookie_path)
            cookie_path.chmod(0o600)
        meta = {
            "saved_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "app_flow": client.app_flow,
        }
        meta_path = self.raw_dir / "session.json"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        meta_path.chmod(0o600)
        self._note(
            "session cookies + appFlow saved (0600, git-ignored) - run "
            "`myusage-archive probe --recheck` TOMORROW to answer the session-lifetime probe"
        )

    def _analyze(self) -> None:
        probes: dict[str, Any] = {}
        signatures: dict[str, Any] = {}

        history = self.pages.get("02-history-default.html")
        if history:
            probes["P4_default_window"] = _daily_table_dates(history)
            probes["P5_services_meters"] = analyze_p5_services_meters(history)

        view15 = view_hourly = None
        page15 = self.pages.get("03-grid15.html")
        if page15:
            matrix = grid_to_matrix(page15, GRID_15MIN_ID)
            if matrix:
                view15 = read_grid(matrix, GRID_15MIN_ID)
                signatures[GRID_15MIN_ID] = layout_signature(view15, len(matrix))
                probes["P1_received_midday"] = analyze_p1_received_midday(view15)
                probes["P2_trailing_rows_15min"] = analyze_p2_trailing_rows(view15)
            else:
                probes["P1_received_midday"] = {"answer": "15-min grid table not found on page"}

        page_hourly = self.pages.get("04-gridhourly.html")
        if page_hourly:
            matrix = grid_to_matrix(page_hourly, GRID_HOURLY_ID)
            if matrix:
                view_hourly = read_grid(matrix, GRID_HOURLY_ID)
                signatures[GRID_HOURLY_ID] = layout_signature(view_hourly, len(matrix))
                probes["P2_trailing_rows_hourly"] = analyze_p2_trailing_rows(view_hourly)

        if view15 and view_hourly:
            probes["P10_label_alignment"] = analyze_p10_alignment(view15, view_hourly)

        for name, key in (
            ("05-post-electric-25mo.html", "P4_post_electric_25_months"),
            ("06-post-electric-60d.html", "P4_post_electric_60_days"),
        ):
            page = self.pages.get(name)
            if page:
                probes[key] = _daily_table_dates(page)

        settings_pages = {n: h for n, h in self.pages.items() if n.startswith("07-settings")}
        probes["P8_mfa_strings"] = analyze_p8_mfa(settings_pages)

        landing = self.pages.get("01-landing.html")
        if landing:
            probes["P11_landing"] = classify_page(landing)

        self.report["probes"] = probes
        self.report["layout_signatures"] = signatures

    def _write_anonymized_bundle(self) -> None:
        anonymizer = Anonymizer(email=self.email, extra_values=self.scrub_extra)
        for html in self.pages.values():
            anonymizer.learn_meters_from_html(html)
        self.anon_dir.mkdir(parents=True, exist_ok=True)
        written: dict[str, str] = {}
        for name, html in self.pages.items():
            cleaned = anonymizer.apply(html)
            (self.anon_dir / name).write_text(cleaned, encoding="utf-8")
            written[name] = cleaned
        report_text = anonymizer.apply(json.dumps(self.report, indent=2, default=str))
        written["probe-report.json"] = report_text
        self._note(f"anonymized bundle: {self.anon_dir} ({len(self.pages)} pages)")
        self._note(f"identifiers mapped: {len(anonymizer.mapping)}")

        leaks = scan_for_leaks(written, email=self.email)
        self.report["leak_scan"] = leaks
        if leaks:
            self._note(
                f"LEAK SCAN: {len(leaks)} suspicious value(s) still present - "
                "do NOT share the bundle until reviewed (see leak_scan in the report)"
            )
        else:
            self._note("leak scan: clean (no emails, tokens, or unmapped identifiers found)")
        # Rewrite the report last so it carries the scan result.
        (self.out / "probe-report.json").write_text(
            anonymizer.apply(json.dumps(self.report, indent=2, default=str)), encoding="utf-8"
        )

    # -- entry points

    async def run(self) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        client = await self._login_canonical()
        if client is None:
            self._write_report_only()
            return self.out / "probe-report.json"
        session = self._keeper_session
        assert session is not None
        try:
            if client.landing_html and not self.grids_only:
                self._save_raw("01-landing.html", client.landing_html)
                self.report.setdefault("captures", {})["01-landing.html"] = classify_page(
                    client.landing_html
                )

            if self.grids_only:
                # Lean repeat-capture path (e.g. the DST window): one login, the two
                # interval grids, nothing else. ~3 requests instead of ~12.
                await self._capture("03-grid15.html", client.fetch_interval_page())
                await self._capture("04-gridhourly.html", client.fetch_hourly_page())
                self._analyze()
                self._write_anonymized_bundle()
                return self.out / "probe-report.json"

            history = await self._capture("02-history-default.html", client.fetch_history_page())
            await self._capture("03-grid15.html", client.fetch_interval_page())
            await self._capture("04-gridhourly.html", client.fetch_hourly_page())

            if history:
                today = dt.date.today()  # noqa: DTZ011 - portal dates are Eastern-naive
                far_back = (today - dt.timedelta(days=25 * 30)).strftime("%m/%d/%Y")
                recent = (today - dt.timedelta(days=60)).strftime("%m/%d/%Y")
                tomorrow = (today + dt.timedelta(days=1)).strftime("%m/%d/%Y")
                await self._capture(
                    "05-post-electric-25mo.html",
                    client.post_daily_history(far_back, tomorrow, "Electric", history),
                )
                await self._capture(
                    "06-post-electric-60d.html",
                    client.post_daily_history(recent, tomorrow, "Electric", history),
                )

                for i, href in enumerate(_discover_settings_links(history), start=1):
                    url = href if href.startswith("http") else f"https://www.myusage.com/{href.lstrip('/')}"
                    await self._capture(f"07-settings-{i}.html", client.fetch_app_url(url))

            await self._persist_session(session, client)
        finally:
            await session.close()

        if not self.skip_auth_matrix:
            await self._auth_variants()

        self._analyze()
        self._write_anonymized_bundle()
        return self.out / "probe-report.json"

    def _write_report_only(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        text = scrub_text(
            json.dumps(self.report, indent=2, default=str), [self.password, self.email]
        )
        (self.out / "probe-report.json").write_text(text, encoding="utf-8")

    async def recheck(self) -> Path:
        """Next-day session-lifetime probe (P7): reuse saved cookies + appFlow."""
        meta_path = self.raw_dir / "session.json"
        cookie_path = self.raw_dir / "session.cookies"
        if not meta_path.exists() or not cookie_path.exists():
            raise MyUsageError("no saved session found - run `myusage-archive probe` first")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

        jar = aiohttp.CookieJar()
        jar.load(cookie_path)
        session = aiohttp.ClientSession(cookie_jar=jar)
        result: dict[str, Any] = {
            "session_saved_at": meta.get("saved_at"),
            "rechecked_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        try:
            client = MyUsageClient(self.email, self.password, session)
            client._app_flow = meta.get("app_flow")  # noqa: SLF001 - deliberate replay
            try:
                html = await client.fetch_interval_page()
                result["old_session_fetch"] = classify_page(html)
                result["old_session_alive"] = bool(result["old_session_fetch"]["grid_tables"])
            except MyUsageError as err:
                result["old_session_alive"] = False
                result["old_session_error"] = scrub_text(str(err), [self.password, self.email])
        finally:
            await session.close()

        report_path = self.out / "probe-recheck.json"
        report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return report_path
