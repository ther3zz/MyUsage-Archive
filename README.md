# myusage-archive

Unofficial personal-archiving tool for utility accounts on Exceleron's
**MyUsage** portal (`myusage.com`), starting with **OUC** (Orlando Utilities
Commission) postpaid electric accounts — including rooftop-solar accounts with
separate *kWh Delivered* / *kWh Received* registers.

The portal exposes 15-minute interval data only through a **rolling 7-day
window**: anything not fetched within that window is permanently lost. This
project logs in as *you*, archives your own data durably to SQLite, and (next
milestone) feeds the Home Assistant Energy dashboard with grid consumption and
grid return statistics.

> **Unaffiliated.** This project is not associated with, endorsed by, or
> supported by Exceleron Software, LLC or Orlando Utilities Commission.
> *MyUsage* is a mark of Exceleron Software. Use it only against your own
> account, at your own risk.

## Status

| Milestone | State |
|-----------|-------|
| M0 — probe harness, portal facts verified live | done |
| M1 — strict parser, SQLite archive, daily fetch loop, CLI | done |
| M2 — Home Assistant integration (Energy dashboard statistics) | **done, awaiting first production install** |
| M3 — daily-history backfill (~15 months available), repair tooling, HACS default store | planned |
| M4 — DST validation (Nov 2026 capture), non-solar/multi-meter fixtures | planned |

## Home Assistant integration (M2)

The integration lives in `custom_components/myusage_archive/` in this same
repository and depends on the library through its manifest pin
(`myusage-archive==0.1.0` on PyPI). It runs one fetch cycle per day at an
Eastern wall-clock time you choose, archives to
`/config/myusage_archive/<entry_id>.db` (included in Home Assistant backups,
with a backup hook that keeps the file consistent), and exports two external
long-term statistics per meter:

| Statistic | Energy dashboard slot |
|-----------|-----------------------|
| `myusage_archive:<meter>_energy_delivered` | Grid consumption |
| `myusage_archive:<meter>_energy_received` | Return to grid |

The sums are an exact decimal fold over the archive, so re-runs are no-ops,
portal corrections re-import contiguously from the changed hour, and a
deleted or restored recorder is rebuilt from the archive instead of producing
negative bars. No solar-production statistic is exported: the portal exposes
export, not gross generation.

### Install on Home Assistant OS

1. **Publish the library** (once per library version; HAOS installs manifest
   requirements from PyPI only): `uv build && uv publish` with your PyPI token.
2. Copy `custom_components/myusage_archive/` into `/config/custom_components/`
   (Samba or SSH add-on) and restart Home Assistant.
3. Settings → Devices & services → Add integration → **MyUsage Archive**.
   Use your `myusage.com` email and password (not the OUC login).
4. After the first cycle, add the two statistics above in Settings → Dashboards
   → Energy. Statistics appear once the recorder has processed the import.
5. Options: fetch time (Eastern), jitter, and how many successfully parsed raw
   pages to retain (failed pages are always kept for diagnosis).

Take a full backup before the first install. The blast radius of an exporter
bug is the two statistic ids above, which can be deleted under Settings →
Developer tools → Statistics and are rebuilt on the next run.

### Diagnostics

Four diagnostic sensors: last successful fetch, newest archived interval,
permanently missing intervals, recoverable missing intervals. Repair issues
are raised for layout changes, unsupported accounts, a halted export, and a
stale archive. The diagnostics download includes the archive summary, recent
fetches, gaps, consistency checks and exporter state, with credentials
redacted.

### Migrating from `dstamen/myusage-ha`

That integration misreads solar accounts (fixed column indices) and rewrites
its statistics with a different baseline every poll. To switch:

1. Remove its config entry and uninstall it (running both doubles portal load).
2. Delete its orphaned statistics under Settings → Developer tools →
   Statistics: `myusage:electric_kwh`, `myusage:water_gal`,
   `myusage:reclaimed_gal`, plus `sensor.myusage_electric_grid` /
   `sensor.myusage_water_grid` if you ran a 1.2.6 beta.
3. Point the Energy dashboard at the `myusage_archive:` statistics.

Deleting *this* integration's statistics does not remove them permanently:
they are rebuilt from the archive on the next cycle. Remove the config entry
instead.

## Daily archiving (M1)

```bash
uv venv .venv && uv pip install -p .venv/bin/python -e .
export MYUSAGE_EMAIL="you@example.com"
# password: prompted, or MYUSAGE_PASSWORD — note that double-quoted exports
# mangle passwords containing ! $ ` \ ; use `read -rs` or the prompt instead.

.venv/bin/myusage-archive login-test
.venv/bin/myusage-archive fetch          # daily table + 15-minute grid → ./myusage-archive.db
.venv/bin/myusage-archive status         # row counts, meter, recent fetches, integrity
.venv/bin/myusage-archive gaps           # per-day completeness; recoverable vs PERMANENT
.venv/bin/myusage-archive verify         # intervals vs daily table; estimated-day flags
.venv/bin/myusage-archive export-csv --out readings.csv
```

Run `fetch` once a day, after the portal's daily batch (about 10:28 AM
Eastern). One cycle is three requests. Anything more frequent adds load without
adding data, since the portal publishes once a day with a two-day lag.

```cron
# 18:00 UTC = 1 PM EST / 2 PM EDT; credentials from a 0600 env file
0 18 * * * cd /path/to/exceleron-client && set -a && . $HOME/.myusage.env && set +a \
  && .venv/bin/myusage-archive fetch >> archive.log 2>&1
```

Missing a day is recoverable for about a week; the `gaps` report says exactly
which intervals are still fetchable and which are gone.

### What the archive guarantees

- **Exact numbers.** Values are stored as decimal text, never floats, so the
  hourly rollup is an exact decimal fold that reproduces the portal's own
  hourly table digit for digit (verified against live captures).
- **Nothing is ever silently zeroed or overwritten.** An empty cell is stored
  as absent. A later fetch with an empty cell cannot erase archived data. Any
  changed value is written to a `revisions` audit table and logged.
- **Failures are kept.** A page that fetches but does not parse is stored raw
  with the error, so a portal layout change can be diagnosed afterwards and
  re-ingested with `reparse` once the parser is fixed.
- **DST is handled.** Eastern's 23- and 25-hour days are modeled explicitly;
  the parser refuses impossible layouts in normal weeks and records anomalies
  instead of crashing during the one week a year the fall-back layout is
  observable.

### Portal facts the code relies on (verified 2026-09-07/08)

- Interval row labels are interval **starts**.
- The daily table's `Type` column is an open vocabulary: `Valid`,
  `Historical`, `Failed` all occur. `Failed` rows are zero-length placeholders
  whose usage rolls into the next successful read.
- Both interval grids and the daily table end with `Total` and `Average`
  summary rows.
- The interval grids abbreviate headers (`kWh Del`, `kWh Rcvd`); the daily
  table spells them out.

## Probe harness (M0)

`myusage-archive probe` captures the portal pages, answers the open
portal-behaviour questions, and writes an anonymized fixture bundle plus a
leak-scanned `probe-report.json`. `probe --grids-only` is the lean variant for
scheduled repeat captures (one login, two grids). See `docs/` and the
implementation plan for the probe questions and their answers.

Everything under `probes/` is git-ignored: the first live bundle leaked an
account number and an internal meter id before the anonymizer was hardened.
Promote reviewed fixtures into `tests/fixtures/` deliberately.

## Design ground rules

- **Fail loudly.** Unknown layouts raise typed errors; no value is ever
  silently defaulted to `0.0`.
- **Header-driven parsing only** — no fixed column indices (the prior art,
  `dstamen/myusage-ha`, misreads solar accounts precisely because of them).
- **All portal times are US/Eastern**, converted explicitly; never stamped UTC.
- **The archive is never touched from an event loop.** Home Assistant cannot
  detect a blocking SQLite call, so the library raises `BlockingCallError`
  itself; the pipeline hands all archive work to a worker thread.
- Credentials are never logged, never written to the repo, and never accepted
  silently on the command line (`--password` warns; prefer the env var or
  prompt).

## Automated access and terms (facts, not legal advice)

- `myusage.com`'s [Terms & Conditions](https://www.myusage.com/terms)
  (Exceleron Software, LLC) contain **no clause about automated access,
  scripts, robots, or scrapers** — there is no permitted-use section at all —
  and its `robots.txt` disallows nothing (both checked 2026-09-07).
  Exceleron's [privacy policy](https://www.myusage.com/privacy) recognizes a
  right of access and data portability for your own usage data.
- `ouc.com`'s [terms](https://www.ouc.com/terms-and-conditions/) **do**
  prohibit automated tools "to navigate or search this Site" — scoped to the
  OUC website. **This tool therefore authenticates directly at
  `myusage.com` (register a MyUsage login with your email if you normally use
  the myOUC button) and never touches `ouc.com`.**

## Development

```bash
uv venv .venv && uv pip install -p .venv/bin/python -e ".[test]"
.venv/bin/python -m pytest -q        # library: 157 tests, no network; live fixtures in tests/fixtures/live
uvx ruff check src tests custom_components tests_ha
.venv/bin/python -m mypy src         # strict

# Home Assistant integration tests need HA's Python (3.14+):
uv venv .venv-ha -p 3.14 && uv pip install -p .venv-ha/bin/python -e . -r requirements-ha-test.txt
.venv-ha/bin/python -m pytest tests_ha -q          # against a real in-memory recorder
```

Live-portal tests are marked `network` and excluded by default. CI runs the
library suite on 3.12 and 3.14, the integration suite on 3.14, hassfest and
the HACS validator, and checks that the built wheel excludes the component.
