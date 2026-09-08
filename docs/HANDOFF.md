# Handoff: myusage-archive — continue in a new session

Paste this whole file as the first message of a new session.

---

You are continuing work on **myusage-archive**: an unofficial archiver for Exceleron's
MyUsage portal (used by OUC, Orlando Utilities Commission) plus a Home Assistant custom
integration that feeds the Energy dashboard. The user has a rooftop-solar OUC account,
so both **kWh Delivered** (grid consumption) and **kWh Received** (export) matter.
The portal exposes 15-minute data only in a **rolling 7-day window**; anything not
fetched is gone forever. Correctness over breadth; fail loudly; never guess.

## Where everything is
- Repo: `~/projects/exceleron-client` (one repo for both halves).
  - `src/myusage_archive/` — library + CLI (`myusage-archive fetch|status|gaps|verify|reparse|export-csv|probe|login-test`).
  - `custom_components/myusage_archive/` — the HA integration. **The library is vendored** into
    `custom_components/myusage_archive/vendor/myusage_archive/` by `scripts/vendor.py`
    (run it after any `src/` change; CI runs `--check`). The component imports the vendored copy
    (`from .vendor.myusage_archive...`), never the top-level package.
  - `tests/` (library, py3.12 venv `.venv`, 157 tests) and `tests_ha/` (HA integration, py3.14 venv
    `.venv-ha` with Home Assistant 2026.9.1, 15 tests against a real in-memory recorder).
  - `tests/fixtures/live/` — anonymized real captures (solar OUC account, 2026-09-08); `probes/` is
    git-ignored and holds raw captures with the user's real data.
  - Plan (approved, detailed, has all verified facts): `/home/reptar/.claude/plans/agent-brief-myusage-archive-calm-gem.md`.
- Commands:
  - `.venv/bin/python -m pytest -q` · `uvx ruff check src tests custom_components tests_ha scripts` · `.venv/bin/python -m mypy src`
  - `.venv-ha/bin/python -m pytest tests_ha -q -p no:cacheprovider` · `.venv-ha/bin/python -m mypy custom_components/myusage_archive --strict --ignore-missing-imports`
  - `python scripts/vendor.py --check`

## Decisions already made (do not re-litigate)
- One repo (monorepo). Library vendored into the component; **no PyPI**, **no HACS** (repo will live on a
  **private LAN-only Forgejo**; HACS reads public GitHub only). Install = copy the component folder to
  `/config/custom_components` + restart.
- Deployment: Home Assistant OS in a VM, **production** instance. Blast radius of an exporter bug is the two
  statistic ids the integration owns (deletable in Developer tools → Statistics; rebuilt on next run).
- Once-daily fetch at an Eastern wall-clock time (default 12:15 PM + jitter), never hourly.
- Authenticate directly at myusage.com; **never automate ouc.com** (its terms prohibit bots; myusage.com's terms
  have no automation clause; robots.txt permissive).
- Credentials never enter the agent environment/logs/repo. The user runs anything needing a live login
  (`myusage-archive probe`, `login-test`, `fetch`) themselves; the agent consumes anonymized outputs.

## Verified portal facts (live, 2026-09-07/08)
- Login: `GET /` (prime) → `POST /login` form `email`/`password` with XHR headers → JSON `{data, redirect_url}` →
  follow → `data.cfm?appPage=Postpaid&…&appFlow=<timestamp-like>`. Failure = JSON without `redirect_url`
  (status 500 + `{error_str, result}`). All three login variants (prime+XHR, prime-only, XHR-only) work;
  back-to-back logins seconds apart once failed (throttle) — space logins ≥45 s.
- Only two interval views: `View 15 Minute Usage`, `View Hourly Usage`; hourly is a strict subset (verified:
  15-min rollup == hourly grid across all 168 hours). Data lags ~2 days; batch ~10:28 AM Eastern.
- Interval grid = one shared-row table: 7 day columns newest-first, `MM/DD` headers + weekday row, 4 header rows,
  96 data rows + 2 summary rows (`Total`, `Average`). Per-day metric group: `°F, kWh Del, kWh Rcvd, kW`
  (stride 4; hourly grid stride 3, no kW). **Labels are interval STARTS.** `kW` is derived (kWhDel×4).
- Daily table: `Meter|High|Low|Posted|From|To|kWh Delivered|kWh Received|kW|Reading|Type`; read windows are
  ~01:30→~01:30 (not midnight; 02:3x/03:2x in older/winter rows); also ends with `Total`/`Average` rows. `Type` is an open vocabulary:
  `Valid`, `Historical`, `Failed` (+blank). **`Failed` rows are zero-length placeholders** (From==To, kWh 0,
  Reading 0) whose usage rolls into the next successful read. Interval sums reconcile with daily windows within
  daily rounding (<1 kWh).
- Daily history range POST works for Electric (`selectedTimePeriod=4`, FromDate/ToDate MM/DD/YYYY, ServiceType,
  action=Load, the literal `SubmitButtonValidateTimePeriod`, CSRF pair `cf_CSRFToken`/`cf_CSRFToken_web` scraped
  as a whole form). ~15 months available (from 2025-06-05). Default GET = 30 days.
- kWh Received is genuinely non-zero midday (export up to ~2.5 kWh per 15 min). No MFA on the login page.
  A "Select Utility" interstitial exists for multi-utility emails (unsupported → typed error).

## Verified Home Assistant facts (2026.9)
- Long-term statistics are hourly only (import raises on non-top-of-hour). `StatisticMetaData` needs all of
  `mean_type` (NONE), `has_sum`, `name`, `source`(=domain), `statistic_id`, `unit_class` (**"energy"** — it is
  the Energy-dashboard picker gate), `unit_of_measurement` (kWh). Import is an upsert keyed (metadata_id, start)
  but an update REPLACES the row → always send exactly `{start, state, sum}`. `async_add_external_statistics`
  is fire-and-forget; never read back in the same cycle. Dashboard reads only `sum` (delta = bar).
- HA's blocking-call detector does **not** cover sqlite3 → the library raises `BlockingCallError` itself if an
  event loop is running on the calling thread; all archive work goes through `hass.async_add_executor_job`.
- Backups tar `.db` and `.db-wal` sequentially → `backup.py` platform checkpoints + holds `BEGIN IMMEDIATE`.
- ZHA precedent for an integration-owned SQLite file in `/config`. Recorder DB must not be reused.
- HA needs Python 3.14; `get_instance` lives in `homeassistant.helpers.recorder`; `OptionsFlowWithReload` exists;
  reauth uses `_get_reauth_entry()` + `async_update_reload_and_abort()`.
- Custom components must ship expanded `translations/en.json` (strings.json is core-build-only); hassfest silently
  skips missing translation files; brands are in-repo (`brand/icon.png` 256², `icon@2x.png` 512²).

## Lessons learned (each cost real time; do not repeat)
1. Shell: `export PASSWORD="…"` mangles `!`, `$`, backticks. Use the tool's prompt or `read -rs`.
2. Anonymizer: the first bundle leaked the **account number** ("Account #" header) and the **internal numeric
   MeterID** (form option + URL param). Anonymizer now maps ACCT/ID/MTR placeholders and a leak scanner runs on
   every bundle; `probes/` is fully git-ignored; promote fixtures to `tests/fixtures/` deliberately after review.
3. The metric header in the live interval grid is `kWh Del`, not `kWh Delivered` — canonicalize on a prefix.
4. `sqlite3.executescript()` commits any open transaction first → put `BEGIN…COMMIT` inside the script.
5. Closures in loops / `except … as err` blocks: bind values (`functools.partial` or default args) before
   awaiting — `err` is unbound after the block, loop vars are late-bound (ruff B023).
6. `localize()` must treat a row label as **wall-clock time**, not elapsed minutes: on the fall-back day every
   post-transition row would otherwise land an hour early. Subtracting two aware datetimes in the same zone gives
   wall-clock difference, not elapsed time — go through UTC.
7. DST week parsing is deliberately lenient (record anomalies, don't raise): the fall-back layout is observable only
   ~Nov 3–9 each year; a crash loop there loses the only annual chance. Normal weeks stay strict.
8. Shared-row grid: row-count checks are table-level; per-day gaps are empty cells, never LayoutErrors.
9. Planner precedence: a **known** archive change at/before the recorder anchor → contiguous reimport from that
   hour; only an **unexplained** anchor-sum mismatch → full rebuild; recorder ahead of archive → halt + issue.
   Sums are a Decimal fold over decimal-text storage (never REAL); ε = 1e-6.
10. NULL never overwrites an archived value; NULL→value fills are revisions; revisions + late rows drive the
    exporter's `earliest_interval_change_since`. Watermark is 1-second resolution (tests must fake "later").
11. HA test harness: `recorder_mock` must be created **before** `hass` (an autouse fixture requesting
    `recorder_db_url` first solves it); the harness reuses ONE config dir for all tests → use unique entry ids and
    delete archives per test; leftover timers fail tests → cancel the coordinator timer on setup failure.
12. `type X = …` aliases trip ruff's py311 target; use a plain assignment after the class.
13. HA tests must monkeypatch the **vendored** module path (`custom_components.myusage_archive.vendor.…`), not
    the top-level package — a stale patch target made `test_revision_triggers_contiguous_reimport` fail after
    vendoring. `python` is not on PATH here; use `.venv/bin/python scripts/vendor.py`.
14. myusage-ha (prior art) is wrong on solar (fixed indices) and rewrites statistics with a new baseline every poll;
    its orphaned ids: `myusage:electric_kwh`, `myusage:water_gal`, `myusage:reclaimed_gal`. Its Tampa Electric
    claim has no evidence.

## Verified 2026-09-08 (M3 session): day attribution
- The daily page embeds its own per-day chart (`accessibleDescription`). Summing rows by
  **usage day = date(To) if To is at/after noon Eastern else date(To) − 1** reproduces it exactly across all
  three captures (545 rows, 0 mismatches). Reads close ~01:30–03:35 (winter 03:2x) and occasionally 22:00–23:00.
  This is `timeutil.usage_day`; `DailyRead.usage_date_local` now means this (schema v2 migration recomputes).
- `Failed` rows: delivered 0, reading 0, **kWh Received sometimes non-zero** (e.g. 23 on 2026-08-13). Exported
  verbatim; delivered rolls into the following 48 h read, which lands on the next day (portal does the same).
- Multiple reads can close on one day (2025-09-10: 44 h Historical + 3.8 h Valid) → summed into one bucket.

## Current state
- Everything is on `main` at Forgejo (`408d66c` M0+M1, `767f754` M2, `fad1d61` vendoring, `3dd13db` M3, then
  the install script). Milestone branches were deleted after fast-forward merges. **Installed on the
  production HA instance 2026-09-08** via `scripts/install-exceleron-client.sh` (run from the HA SSH add-on,
  BusyBox userland — no rsync, no GNU chown).
- M3 built: `series.py` plans over *points* (hour or midnight day bucket; `build_points` eligibility = zero
  intervals AND whole day before `oldest_recoverable_utc`), `merge_stray_rows` rewrites recorder starts a
  full/reimport no longer produces (plan §5 flip rule), `Archive.daily_buckets/daily_change_days_since/
  range_fetch_covered_from_utc`, `Pipeline.backfill_daily` + `run_cycle(backfill_days=)` (kind `daily_range`,
  records the *requested* window so it is one-shot per span), CLI `backfill --days`, HA option
  `backfill_days` (default 730, 0 = off), diagnostics `hour_points/day_points`.
- All green: 178 library tests, 18 HA tests, ruff, mypy --strict (both), vendor check.
- Manifest points at the public GitHub mirror `https://github.com/ther3zz/MyUsage-Archive` (Forgejo is the private origin and push-mirrors
  to it); the private hostname must never appear in the tree or history.

## Next steps, in order
1. User: confirm the first cycle ran (log lines for backfill + export), wire
   `myusage_archive:<meter>_energy_delivered` (grid consumption) and `…_energy_received` (return to grid) in
   the Energy dashboard; updates = re-run the install one-liner in the README + restart.
2. User: run `myusage-archive probe --recheck` once (session-lifetime probe); switch the Nov 3–9 2026 cron entry to
   `probe --grids-only`; run `myusage-archive fetch` daily via cron if not using HA for a while.
3. Agent: M4 — Nov 2026 DST fold validation, non-solar/multi-meter fixtures, optional water. Possible M3
   follow-ups: a `plan` CLI dry-run needs a recorder so it stays HA-only; the `gaps` report could list
   permanent days that have neither intervals nor a daily row (holes the backfill cannot fill).

## Open unknowns
P3 non-solar daily layout (10-col single kWh — unverified fixture), P6 DST rendering (Nov 2026),
P9 how `Failed` reads look inside the interval grids, session/appFlow lifetime.

## Hard rules
Never take the user's credentials; never commit `probes/`, `*.db`, `.env*`, `*.cookies`; never touch ouc.com;
never substitute 0.0 for a missing value; never rewrite statistics with a new baseline; keep the once-daily cadence.
