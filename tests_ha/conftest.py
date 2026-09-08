"""Home Assistant integration tests (run with the Python 3.14 venv):

    .venv-ha/bin/python -m pytest tests_ha -q

Kept out of ``tests/`` so the fast library suite never imports homeassistant.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myusage_archive.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN
from custom_components.myusage_archive.coordinator import archive_path
from custom_components.myusage_archive.vendor.myusage_archive.archive import Archive
from custom_components.myusage_archive.vendor.myusage_archive.parser import (
    parse_daily_history,
    parse_interval_grid,
)

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures" / "live"
REF = dt.date(2026, 9, 8)
METER = "MTR001"


@pytest.fixture(autouse=True)
def _recorder_url_before_hass(recorder_db_url: str) -> None:
    """The harness asserts recorder_db_url is created before `hass`; autouse
    fixtures run in definition order, so this one must come first."""


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Generator[None]:
    yield


@pytest.fixture
def entry(hass) -> Generator[MockConfigEntry]:
    """A config entry with a unique id per test.

    The harness reuses one config directory across tests, so archives would
    otherwise leak from test to test (and satisfy the startup freshness guard).
    """
    entry_id = f"entry_{uuid.uuid4().hex[:12]}"
    yield MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        unique_id="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pw"},
        entry_id=entry_id,
    )
    base = Path(archive_path(hass, entry_id))
    for suffix in ("", "-wal", "-shm"):
        Path(str(base) + suffix).unlink(missing_ok=True)


def seed_archive(path: str) -> Archive:
    """Populate an archive from the live captures, marking the fetch as just now."""
    archive = Archive(path, allow_event_loop=True)
    grid = parse_interval_grid((FIXTURES / "03-grid15.html").read_text(), reference_date=REF)
    fid = archive.record_fetch("grid15", ok=True, http_status=200, layout_signature=grid.signature)
    archive.store_intervals(grid.readings, meter=METER, fetch_id=fid, has_received=True)
    daily = parse_daily_history((FIXTURES / "06-post-electric-60d.html").read_text())
    archive.store_daily(daily.reads, fetch_id=archive.record_fetch("daily", ok=True))
    return archive


@pytest.fixture
def seeded(hass, entry: MockConfigEntry) -> Archive:
    return seed_archive(archive_path(hass, entry.entry_id))
