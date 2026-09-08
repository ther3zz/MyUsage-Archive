"""Strict, header-driven parsers for the MyUsage grids.

Design rules, all of which exist because the prior art (`dstamen/myusage-ha`)
violated them and produced silently wrong numbers on solar accounts:

* **Never index columns positionally.** Every column's meaning comes from its
  header text, normalized through one canonical vocabulary.
* **Measure the per-day metric stride; never assume it.** The interval grids
  repeat a metric group per day column. The group width is derived from the
  header, so a layout with or without temperature or demand both work.
* **Fail loudly.** An unrecognized header, an inconsistent stride or an
  impossible row count raises `LayoutError`. A cell that should be a number
  but is not raises `DataError`. Nothing is ever defaulted to zero.
* **Absent is not zero.** An empty cell becomes `None`, recorded as a gap.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup
from bs4.element import Tag

from .const import GRID_15MIN_ID, GRID_DAILY_ID, GRID_HOURLY_ID
from .exceptions import DataError, LayoutError
from .models import (
    METRIC_KW,
    METRIC_KWH,
    METRIC_KWH_DELIVERED,
    METRIC_KWH_RECEIVED,
    METRIC_TEMP_F,
    DailyHistory,
    DailyRead,
    IntervalGrid,
    IntervalReading,
    LayoutSignature,
    Resolution,
)
from .timeutil import (
    eastern_today,
    expected_interval_count,
    has_dst_transition,
    is_time_label,
    localize,
    parse_local_datetime,
    parse_mmdd,
    parse_time_label,
    slot_exists,
)

# Row labels in the first column that mark a summary row rather than data.
# Confirmed live 2026-09-08: both interval grids and the daily table carry
# trailing "Total" and "Average" rows.
SUMMARY_LABELS = frozenset({"total", "average", "totals", "avg", "sum"})

_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Normalize header text: collapse whitespace/nbsp, strip, casefold."""
    return _WS_RE.sub(" ", text.replace("\xa0", " ")).strip().casefold()


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _norm(text))


def canonical_metric(text: str) -> str | None:
    """Map a metric header to a canonical name, or None if unrecognized.

    Handles both dialects seen live: the daily table spells out
    "kWh Delivered"/"kWh Received"; the interval grids abbreviate to
    "kWh Del"/"kWh Rcvd".
    """
    normalized = _norm(text)
    compact = _compact(text)
    if not compact:
        return None
    if "°" in text or compact in {"f", "degf", "tempf", "temp", "temperature"}:
        return METRIC_TEMP_F
    if compact.startswith("kwh"):
        rest = compact[3:]
        if not rest:
            return METRIC_KWH
        if rest.startswith("del"):
            return METRIC_KWH_DELIVERED
        if rest.startswith(("rec", "rcv")):
            return METRIC_KWH_RECEIVED
        return None
    if compact == "kw":
        return METRIC_KW
    if "deliver" in normalized:
        return METRIC_KWH_DELIVERED
    if "receiv" in normalized or "rcvd" in normalized:
        return METRIC_KWH_RECEIVED
    return None


# Daily-table column vocabulary.
_DAILY_COLUMNS: dict[str, tuple[str, ...]] = {
    "meter": ("meter",),
    "high": ("high",),
    "low": ("low",),
    "posted": ("posted",),
    "from": ("from",),
    "to": ("to",),
    "kwh_delivered": ("kwhdelivered", "kwhdel"),
    "kwh_received": ("kwhreceived", "kwhrcvd"),
    "kwh": ("kwh",),
    "kw": ("kw",),
    "reading": ("reading",),
    "type": ("type",),
}


def _cell_text(cell: Tag) -> str:
    return _WS_RE.sub(" ", cell.get_text(" ", strip=True).replace("\xa0", " ")).strip()


def _cell_decimal(cell: Tag, *, where: str) -> Decimal | None:
    """Read a numeric cell. Empty means missing (None); garbage raises."""
    raw = cell.get("data-raw-value")
    candidates = [raw] if isinstance(raw, str) and raw.strip() else []
    candidates.append(_cell_text(cell))
    for candidate in candidates:
        cleaned = candidate.replace(",", "").replace("°", "").replace("$", "").strip()
        if cleaned in {"", "-", "--", "—", "N/A", "n/a"}:
            continue
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            raise DataError(f"{where}: cannot parse {candidate!r} as a number") from None
    return None


def _rows(table: Tag) -> list[list[Tag]]:
    out: list[list[Tag]] = []
    for row in table.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        cells = [c for c in row.find_all(("th", "td")) if isinstance(c, Tag)]
        if cells:
            out.append(cells)
    return out


def _find_table(html: str, table_id: str) -> Tag:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=table_id)
    if not isinstance(table, Tag):
        present = [
            str(t.get("id"))
            for t in soup.find_all("table")
            if isinstance(t, Tag) and t.get("id")
        ]
        raise LayoutError(f"table id={table_id!r} not found; tables present: {present}")
    return table


def _is_summary_label(label: str) -> bool:
    return _norm(label) in SUMMARY_LABELS


# --------------------------------------------------------------------- daily


def parse_daily_history(html: str) -> DailyHistory:
    """Parse the `gridUsageHistory` table.

    Handles both the solar layout (kWh Delivered + kWh Received) and the
    single-`kWh` non-solar layout. Summary rows are excluded; every other row
    must parse.
    """
    table = _find_table(html, GRID_DAILY_ID)
    rows = _rows(table)
    if not rows:
        raise LayoutError(f"{GRID_DAILY_ID}: table has no rows")

    header_cells = rows[0]
    index: dict[str, int] = {}
    unknown: list[str] = []
    for position, cell in enumerate(header_cells):
        compact = _compact(_cell_text(cell))
        if not compact:
            continue
        for name, aliases in _DAILY_COLUMNS.items():
            if compact in aliases:
                index.setdefault(name, position)
                break
        else:
            unknown.append(_cell_text(cell))
    if unknown:
        raise LayoutError(f"{GRID_DAILY_ID}: unrecognized column header(s): {unknown}")

    required = {"meter", "from", "type"}
    missing = required - index.keys()
    if missing:
        raise LayoutError(f"{GRID_DAILY_ID}: missing required column(s): {sorted(missing)}")
    if not ({"kwh_delivered", "kwh"} & index.keys()):
        raise LayoutError(
            f"{GRID_DAILY_ID}: no usage column (need 'kWh Delivered' or 'kWh')"
        )

    reads: list[DailyRead] = []
    meters: list[str] = []
    excluded: list[str] = []

    for row_no, cells in enumerate(rows[1:], start=1):
        label = _cell_text(cells[0]) if cells else ""
        if _is_summary_label(label):
            excluded.append(label)
            continue
        if len(cells) < len(header_cells):
            raise LayoutError(
                f"{GRID_DAILY_ID} row {row_no}: {len(cells)} cells, "
                f"expected {len(header_cells)} (label {label!r})"
            )

        def col(name: str, cells: list[Tag] = cells) -> Tag | None:
            position = index.get(name)
            return cells[position] if position is not None else None

        where = f"{GRID_DAILY_ID} row {row_no}"
        meter = _cell_text(cells[index["meter"]])
        if not meter:
            raise LayoutError(f"{where}: blank meter cell")
        if meter not in meters:
            meters.append(meter)

        from_cell = col("from")
        assert from_cell is not None
        from_ts = parse_local_datetime(_cell_text(from_cell))

        to_cell = col("to")
        to_text = _cell_text(to_cell) if to_cell is not None else ""
        to_ts = parse_local_datetime(to_text) if to_text else None

        posted_cell = col("posted")
        posted_text = _cell_text(posted_cell) if posted_cell is not None else ""
        posted_ts = parse_local_datetime(posted_text) if posted_text else None

        delivered_cell = col("kwh_delivered") or col("kwh")
        received_cell = col("kwh_received")

        reads.append(
            DailyRead(
                meter=meter,
                from_ts=from_ts,
                to_ts=to_ts,
                posted_ts=posted_ts,
                read_type=_cell_text(cells[index["type"]]),
                kwh_delivered=(
                    _cell_decimal(delivered_cell, where=f"{where} kWh delivered")
                    if delivered_cell is not None
                    else None
                ),
                kwh_received=(
                    _cell_decimal(received_cell, where=f"{where} kWh received")
                    if received_cell is not None
                    else None
                ),
                kw=(
                    _cell_decimal(c, where=f"{where} kW") if (c := col("kw")) is not None else None
                ),
                meter_reading=(
                    _cell_decimal(c, where=f"{where} reading")
                    if (c := col("reading")) is not None
                    else None
                ),
                high_f=(
                    _cell_decimal(c, where=f"{where} high")
                    if (c := col("high")) is not None
                    else None
                ),
                low_f=(
                    _cell_decimal(c, where=f"{where} low")
                    if (c := col("low")) is not None
                    else None
                ),
            )
        )

    return DailyHistory(
        reads=tuple(reads),
        meters=tuple(meters),
        excluded_rows=tuple(excluded),
        columns=tuple(_cell_text(c) for c in header_cells),
    )


# ------------------------------------------------------------------ interval


def _header_block(rows: list[list[Tag]], table_id: str) -> tuple[int, list[str], list[str]]:
    """Locate the metric-header row; return (index, date texts, weekday texts).

    The live grids use a 4-row header: dates, weekdays, chart links, metrics.
    The metric row is identified by its first cell reading Time/Hour, rather
    than by a hardcoded offset.
    """
    metric_row = None
    for position, cells in enumerate(rows[:8]):
        first = _norm(_cell_text(cells[0])) if cells else ""
        if first.startswith(("time", "hour")):
            metric_row = position
            break
    if metric_row is None:
        raise LayoutError(
            f"{table_id}: no Time/Hour header row in the first 8 rows "
            f"(first cells: {[_cell_text(c[0]) for c in rows[:8] if c]})"
        )
    if metric_row == 0:
        raise LayoutError(f"{table_id}: metric header row has no date row above it")

    dates = [_cell_text(c) for c in rows[0][1:]]
    weekdays = [_cell_text(c) for c in rows[1][1:]] if metric_row >= 2 else []
    return metric_row, dates, weekdays


def parse_interval_grid(
    html: str,
    *,
    table_id: str = GRID_15MIN_ID,
    reference_date: dt.date | None = None,
    meter: str | None = None,
) -> IntervalGrid:
    """Parse `grid15MinuteUsage` or `gridHourlyUsage`.

    The grid is one shared-row table: all seven day columns share the same
    time rows, so a day with missing data shows empty *cells*, not fewer rows.
    Row-count checks are therefore table-level; per-day completeness is a gap
    concern, not a parse error.
    """
    reference = reference_date or eastern_today()
    resolution = Resolution.HOURLY if table_id == GRID_HOURLY_ID else Resolution.FIFTEEN_MIN
    minutes = 60 if resolution is Resolution.HOURLY else 15

    table = _find_table(html, table_id)
    rows = _rows(table)
    if len(rows) < 5:
        raise LayoutError(f"{table_id}: only {len(rows)} rows; not a usable grid")

    metric_row_idx, date_texts, weekday_texts = _header_block(rows, table_id)

    # Resolve day columns.
    dates: list[dt.date] = []
    for position, text in enumerate(date_texts):
        if not text:
            continue
        weekday = weekday_texts[position] if position < len(weekday_texts) else None
        dates.append(parse_mmdd(text, reference, weekday))
    if not dates:
        raise LayoutError(f"{table_id}: no MM/DD day headers found (row 0: {date_texts})")

    # Measure the per-day metric stride. No anchor metric is assumed, so a
    # layout without a temperature column parses identically.
    metric_cells = [_cell_text(c) for c in rows[metric_row_idx][1:]]
    if len(metric_cells) % len(dates) != 0:
        raise LayoutError(
            f"{table_id}: {len(metric_cells)} metric cells is not divisible by "
            f"{len(dates)} day columns"
        )
    stride = len(metric_cells) // len(dates)
    if stride == 0:
        raise LayoutError(f"{table_id}: metric header row is empty")

    groups: list[tuple[str, ...]] = []
    for day_index in range(len(dates)):
        chunk = metric_cells[day_index * stride : (day_index + 1) * stride]
        canonical: list[str] = []
        for text in chunk:
            name = canonical_metric(text)
            if name is None:
                raise LayoutError(
                    f"{table_id}: unrecognized metric header {text!r} "
                    f"(day column {day_index}, group {chunk})"
                )
            canonical.append(name)
        groups.append(tuple(canonical))
    if len(set(groups)) != 1:
        raise LayoutError(f"{table_id}: per-day metric groups differ: {sorted(set(groups))}")
    metrics = groups[0]
    if not ({METRIC_KWH_DELIVERED, METRIC_KWH} & set(metrics)):
        raise LayoutError(f"{table_id}: no usage metric in per-day group {metrics}")

    metric_pos = {name: i for i, name in enumerate(metrics)}

    # Split body rows into data rows and excluded summary rows.
    body = rows[metric_row_idx + 1 :]
    data_rows: list[list[Tag]] = []
    excluded: list[str] = []
    for cells in body:
        label = _cell_text(cells[0]) if cells else ""
        if is_time_label(label):
            data_rows.append(cells)
        else:
            excluded.append(label)

    if not data_rows:
        raise LayoutError(f"{table_id}: no time-labelled data rows (excluded: {excluded})")

    # Table-level count check against the longest day in the window. The grid
    # shares rows across day columns, so per-day completeness is a gap concern,
    # not a parse error.
    #
    # DST weeks are deliberately lenient: the portal's rendering of a 23- or
    # 25-hour day is unverified until the Nov 2026 capture, and that window is
    # observable for one week per year. Hard-failing there would lose the only
    # chance to learn the layout, so anomalies are recorded instead.
    anomalies: list[str] = []
    dst_week = has_dst_transition(list(dates))
    expected = max(expected_interval_count(day, minutes) for day in dates)
    if len(data_rows) != expected:
        message = (
            f"{table_id}: {len(data_rows)} data rows, expected {expected} for the "
            f"window {dates[-1].isoformat()}..{dates[0].isoformat()} "
            f"(excluded rows: {excluded})"
        )
        if not dst_week:
            raise LayoutError(message)
        anomalies.append(message + " — tolerated because the window spans a DST change")

    # The label ladder must be ordered. A fall-back day legitimately repeats an
    # hour, so exactly one backward step is allowed in a DST week.
    minute_labels = [parse_time_label(_cell_text(cells[0])) for cells in data_rows]
    backward = [
        (previous, current)
        for previous, current in zip(minute_labels, minute_labels[1:], strict=False)
        if current < previous
    ]
    if backward and not (dst_week and len(backward) == 1):
        raise LayoutError(
            f"{table_id}: time labels are not ordered; backward step(s) {backward}"
        )
    if backward:
        anomalies.append(f"repeated hour in the ladder at {backward[0]} (DST fall-back)")

    readings: list[IntervalReading] = []
    for day_index, day in enumerate(dates):
        seen_minutes: dict[int, int] = {}
        for row_index, cells in enumerate(data_rows):
            label_minutes = minute_labels[row_index]
            occurrence = seen_minutes.get(label_minutes, 0)
            seen_minutes[label_minutes] = occurrence + 1

            base = 1 + day_index * stride
            if base + stride > len(cells):
                raise LayoutError(
                    f"{table_id} row {row_index}: {len(cells)} cells, need "
                    f"{base + stride} for day column {day_index}"
                )
            group_cells = cells[base : base + stride]

            def value(
                name: str,
                group_cells: list[Tag] = group_cells,
                day: dt.date = day,
                row_index: int = row_index,
            ) -> Decimal | None:
                position = metric_pos.get(name)
                if position is None:
                    return None
                return _cell_decimal(
                    group_cells[position],
                    where=f"{table_id} {day.isoformat()} row {row_index} {name}",
                )

            delivered = value(METRIC_KWH_DELIVERED)
            if delivered is None and METRIC_KWH in metric_pos:
                delivered = value(METRIC_KWH)
            received = value(METRIC_KWH_RECEIVED)
            temperature = value(METRIC_TEMP_F)

            # Does this wall-clock slot exist on this particular day? A shared
            # row can address a slot a given day does not have: the repeated
            # hour of a fall-back day seen from a normal day's column, or the
            # skipped hour of a spring-forward day. Such a cell must be empty.
            if not slot_exists(day, label_minutes, occurrence):
                if delivered is not None or received is not None:
                    anomalies.append(
                        f"{day.isoformat()} carries data at "
                        f"{label_minutes // 60:02d}:{label_minutes % 60:02d}"
                        f"{' (2nd pass)' if occurrence else ''}, a slot that does not "
                        "exist on that local day"
                    )
                continue

            start = localize(day, label_minutes, second_pass=occurrence > 0)
            readings.append(
                IntervalReading(
                    meter=meter or "",
                    start=start,
                    resolution=resolution,
                    kwh_delivered=delivered,
                    kwh_received=received,
                    temperature_f=temperature,
                )
            )

    signature = LayoutSignature(
        table_id=table_id,
        day_count=len(dates),
        stride=stride,
        metrics=metrics,
        header_rows=metric_row_idx + 1,
        data_rows=len(data_rows),
        excluded_rows=tuple(excluded),
        total_rows=len(rows),
        anomalies=tuple(anomalies),
    )
    return IntervalGrid(
        readings=tuple(readings),
        signature=signature,
        dates=tuple(dates),
        meter=meter,
    )
