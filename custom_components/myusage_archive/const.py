"""Constants for the MyUsage Archive integration."""

from __future__ import annotations

DOMAIN = "myusage_archive"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"  # noqa: S105 - a key name, not a secret
CONF_FETCH_TIME = "fetch_time"          # "HH:MM", America/New_York wall clock
CONF_JITTER_MINUTES = "jitter_minutes"
CONF_KEEP_RAW_PAGES = "keep_raw_pages"
CONF_BACKFILL_DAYS = "backfill_days"     # one-shot daily-history range fetch span

DEFAULT_FETCH_TIME = "12:15"            # ~2 h after the portal's ~10:28 AM Eastern batch
DEFAULT_JITTER_MINUTES = 10
DEFAULT_KEEP_RAW_PAGES = 3
# The portal keeps ~15 months of daily history (a 25-month request returned
# data from 2025-06-05 on 2026-09-08); asking for two years returns all of it.
DEFAULT_BACKFILL_DAYS = 730
MAX_BACKFILL_DAYS = 1100

# Retry when the scheduled fetch finds no new interval rows (batch not yet
# published) or fails transiently. Short on purpose: data is 2 days old
# anyway, and a missed day is recoverable inside the 7-day portal window.
RETRY_DELAYS_MINUTES: tuple[int, ...] = (60, 180)

# Skip the startup fetch if a successful fetch happened this recently, so a
# restart loop cannot hammer the portal.
STARTUP_FETCH_MIN_AGE_HOURS = 6

# Archive location under the HA config directory: one database per entry.
ARCHIVE_DIR = "myusage_archive"

STAT_DELIVERED = "energy_delivered"
STAT_RECEIVED = "energy_received"

ISSUE_LAYOUT_ERROR = "layout_error"
ISSUE_EXPORT_HALTED = "export_halted"
ISSUE_UNSUPPORTED_ACCOUNT = "unsupported_account"
ISSUE_STALE = "stale_archive"

STALE_AFTER_HOURS = 48
