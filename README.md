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
| M1 — strict parser, SQLite archive, daily fetch loop, CLI | **done** |
| M2 — Home Assistant integration (Energy dashboard statistics) | next |
| M3 — daily-history backfill (~15 months available), repair tooling, HACS | planned |
| M4 — DST validation (Nov 2026 capture), non-solar/multi-meter fixtures | planned |

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
.venv/bin/python -m pytest -q        # 134 tests, no network; live fixtures in tests/fixtures/live
uvx ruff check src tests
.venv/bin/python -m mypy src         # strict
```

Live-portal tests are marked `network` and excluded by default.
