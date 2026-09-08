"""Typed records produced by the parsers.

Values are `Decimal` (never `float`) so that archived data and the cumulative
sums derived from it are exact and reproducible. `None` always means "the
portal did not give us a value here" — never zero.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from .timeutil import usage_day


class Resolution(StrEnum):
    """Interval resolutions the portal exposes."""

    FIFTEEN_MIN = "15min"
    HOURLY = "hourly"


# Canonical metric names. The portal spells these differently in different
# tables ("kWh Delivered" in the daily grid, "kWh Del" in the interval grids),
# so parsing normalizes to these.
METRIC_TEMP_F = "temp_f"
METRIC_KWH_DELIVERED = "kwh_delivered"
METRIC_KWH_RECEIVED = "kwh_received"
METRIC_KW = "kw"
METRIC_KWH = "kwh"  # non-solar single-column layout

KNOWN_METRICS = frozenset(
    {METRIC_TEMP_F, METRIC_KWH_DELIVERED, METRIC_KWH_RECEIVED, METRIC_KW, METRIC_KWH}
)


@dataclass(frozen=True, slots=True)
class IntervalReading:
    """One interval for one meter. `start` is the interval START, local-aware.

    Verified 2026-09-08 (probe P10): the portal's row labels are interval
    starts, not ends.
    """

    meter: str
    start: dt.datetime  # timezone-aware
    resolution: Resolution
    kwh_delivered: Decimal | None = None
    kwh_received: Decimal | None = None
    temperature_f: Decimal | None = None

    @property
    def start_utc(self) -> int:
        return int(self.start.timestamp())

    @property
    def is_empty(self) -> bool:
        """True when the portal rendered no usage values for this slot."""
        return self.kwh_delivered is None and self.kwh_received is None


@dataclass(frozen=True, slots=True)
class DailyRead:
    """One row of the daily Usage History table.

    `read_type` is stored verbatim: the live portal emits at least `Valid`,
    `Historical`, `Failed` and empty, so this is an open vocabulary, not an
    enum. `Failed` rows are zero-length placeholders (from == to, zero
    delivered usage and zero reading) whose delivered consumption rolls into
    the next successful read; the live portal still shows a non-zero
    `kWh Received` on some of them, so no field is ever assumed to be zero.
    """

    meter: str
    from_ts: dt.datetime
    to_ts: dt.datetime | None
    posted_ts: dt.datetime | None
    read_type: str
    kwh_delivered: Decimal | None = None
    kwh_received: Decimal | None = None
    kw: Decimal | None = None
    meter_reading: Decimal | None = None
    high_f: Decimal | None = None
    low_f: Decimal | None = None

    @property
    def is_failed(self) -> bool:
        return self.read_type.strip().casefold() == "failed"

    @property
    def is_zero_length(self) -> bool:
        return self.to_ts is not None and self.to_ts == self.from_ts

    @property
    def read_end(self) -> dt.datetime:
        """When the window closed; a placeholder without a ``To`` closes at ``From``."""
        return self.to_ts if self.to_ts is not None else self.from_ts

    @property
    def usage_date_local(self) -> dt.date:
        """The local date the portal attributes this window to.

        Follows the portal's own chart (see :func:`timeutil.usage_day`): the
        date is decided by when the read *closed*, so a ``Failed`` placeholder
        and the multi-day read that follows it land on consecutive days,
        exactly as the portal draws them.
        """
        return usage_day(self.read_end)


@dataclass(frozen=True, slots=True)
class LayoutSignature:
    """Structural fingerprint of a parsed grid — cheap drift telemetry."""

    table_id: str
    day_count: int
    stride: int
    metrics: tuple[str, ...]
    header_rows: int
    data_rows: int
    excluded_rows: tuple[str, ...]
    total_rows: int
    anomalies: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "table_id": self.table_id,
            "day_count": self.day_count,
            "stride": self.stride,
            "metrics": list(self.metrics),
            "header_rows": self.header_rows,
            "data_rows": self.data_rows,
            "excluded_rows": list(self.excluded_rows),
            "total_rows": self.total_rows,
            "anomalies": list(self.anomalies),
        }


@dataclass(frozen=True, slots=True)
class IntervalGrid:
    """A parsed interval grid: readings plus the layout they came from."""

    readings: tuple[IntervalReading, ...]
    signature: LayoutSignature
    dates: tuple[dt.date, ...]
    meter: str | None = None

    @property
    def has_received(self) -> bool:
        return METRIC_KWH_RECEIVED in self.signature.metrics


@dataclass(frozen=True, slots=True)
class DailyHistory:
    """Parsed daily table: reads plus the meters seen."""

    reads: tuple[DailyRead, ...]
    meters: tuple[str, ...] = field(default_factory=tuple)
    excluded_rows: tuple[str, ...] = field(default_factory=tuple)
    columns: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_received(self) -> bool:
        return any(r.kwh_received is not None for r in self.reads)
