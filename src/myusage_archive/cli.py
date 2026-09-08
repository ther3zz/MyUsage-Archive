"""Command-line interface.

Network verbs (need credentials):
  myusage-archive login-test              verify credentials + landing page
  myusage-archive fetch                   one archive cycle: daily table + 15-min grid
  myusage-archive backfill [--days N]     one-shot daily-history range fetch (~15 months)
  myusage-archive probe [--grids-only]    M0 probe/capture harness
  myusage-archive probe --recheck         next-day session-lifetime check

Local verbs (archive only, no network):
  myusage-archive status                  archive stats + recent fetches
  myusage-archive gaps                    per-day completeness report
  myusage-archive verify                  intervals vs daily table, estimated days
  myusage-archive reparse                 re-run the parser over retained raw pages
  myusage-archive export-csv              dump 15-minute readings as CSV

Credentials: --email / MYUSAGE_EMAIL / prompt, and MYUSAGE_PASSWORD / getpass
prompt. --password exists for scripting but warns (shell history, `ps`).
The archive path defaults to ./myusage-archive.db (override with --db or
MYUSAGE_DB).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import logging
import os
import sys
from getpass import getpass
from pathlib import Path

import aiohttp

from . import __version__
from .archive import Archive
from .client import MyUsageClient
from .exceptions import AuthenticationError, MyUsageError, UnsupportedAccountError
from .probe import ProbeRunner
from .service import Pipeline, reparse

DEFAULT_DB = "myusage-archive.db"
DEFAULT_BACKFILL_DAYS = 730  # a 25-month request returned ~15 months (2026-09-08)


# ----------------------------------------------------------------- helpers


def _resolve_credentials(args: argparse.Namespace, *, required: bool = True) -> tuple[str, str]:
    email = args.email or os.environ.get("MYUSAGE_EMAIL") or ""
    password = args.password or os.environ.get("MYUSAGE_PASSWORD") or ""
    if args.password:
        print(
            "warning: --password is visible in shell history and `ps`; "
            "prefer MYUSAGE_PASSWORD or the prompt",
            file=sys.stderr,
        )
    if required and not email:
        email = input("MyUsage email: ")
    if required and not password:
        password = getpass("MyUsage password: ")
    return email, password


def _archive(args: argparse.Namespace) -> Archive:
    path = args.db or os.environ.get("MYUSAGE_DB") or DEFAULT_DB
    return Archive(path, keep_ok_raw_pages=args.keep_raw)


def _single_meter(archive: Archive, requested: str | None) -> str:
    meters = [m.meter_number for m in archive.meters()]
    if requested:
        if meters and requested not in meters:
            raise MyUsageError(f"meter {requested!r} not in archive (have {meters})")
        return requested
    if len(meters) != 1:
        raise MyUsageError(
            f"archive has {len(meters)} meters {meters}; pass --meter to choose one"
        )
    return meters[0]


def _fmt_utc(ts: int | None) -> str:
    if ts is None:
        return "-"
    return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(timespec="seconds")


# ------------------------------------------------------------ network verbs


async def _login_test(email: str, password: str) -> int:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = MyUsageClient(email, password, session)
        try:
            await client.login()
        except AuthenticationError as err:
            print(f"AUTH FAILED: {err}")
            return 2
        except UnsupportedAccountError as err:
            print(f"LOGIN OK, ACCOUNT UNSUPPORTED: {err}")
            return 3
        except MyUsageError as err:
            print(f"ERROR: {err}")
            return 1
        print("login OK - landed on the Postpaid app (appFlow acquired)")
        return 0


async def _fetch(email: str, password: str, archive: Archive, meter: str | None) -> int:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = MyUsageClient(email, password, session)
        pipeline = Pipeline(client, archive)
        try:
            result = await pipeline.run_cycle(meter=meter)
        except AuthenticationError as err:
            print(f"AUTH FAILED: {err}")
            return 2
        except MyUsageError as err:
            print(f"ERROR: {err}")
            print("(if this was a parse failure, the raw page was kept: see `status`)")
            return 1

    print(f"meter: {result.meter}")
    if result.daily and result.daily.store:
        d = result.daily.store
        print(f"daily     : fetch {result.daily.fetch_id}: +{d.inserted} new, "
              f"{d.updated} updated, {d.unchanged} unchanged, {d.revisions} revisions")
    s = result.intervals.store
    assert s is not None
    print(f"intervals : fetch {result.intervals.fetch_id}: +{s.inserted} new, "
          f"{s.updated} updated, {s.unchanged} unchanged, {s.revisions} revisions")
    for anomaly in result.intervals.anomalies:
        print(f"  anomaly : {anomaly}")
    if result.gaps:
        g = result.gaps
        print(f"gaps      : {len(g.incomplete_days)} incomplete day(s); "
              f"{g.recoverable_missing} recoverable, "
              f"{g.permanent_missing} PERMANENT intervals missing")
    for issue in result.issues:
        print(f"  check   : {issue}")
    return 0


async def _backfill(email: str, password: str, archive: Archive, days: int) -> int:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = MyUsageClient(email, password, session)
        pipeline = Pipeline(client, archive)
        try:
            await client.login()
            outcome = await pipeline.backfill_daily(days=days)
        except AuthenticationError as err:
            print(f"AUTH FAILED: {err}")
            return 2
        except MyUsageError as err:
            print(f"ERROR: {err}")
            print("(if this was a parse failure, the raw page was kept: see `status`)")
            return 1
    store = outcome.store
    assert store is not None
    print(f"backfill  : fetch {outcome.fetch_id}: +{store.inserted} new, "
          f"{store.updated} updated, {store.unchanged} unchanged, {store.revisions} revisions")
    for meter in outcome.meters:
        buckets = archive.daily_buckets(meter)
        if buckets:
            print(f"  {meter}: daily history {buckets[0].day} .. {buckets[-1].day} "
                  f"({len(buckets)} days)")
    return 0


# -------------------------------------------------------------- local verbs


def _status(archive: Archive) -> int:
    stats = archive.stats()
    print(f"archive : {stats['path']}")
    print(f"          schema v{stats['schema_version']}, journal {stats['journal_mode']}")
    for table, count in stats["rows"].items():
        print(f"  {table:18} {count:>8}")
    for m in archive.meters():
        rng = archive.interval_range(m.meter_number)
        span = f"{_fmt_utc(rng[0])} .. {_fmt_utc(rng[1])}" if rng else "no intervals"
        print(f"meter   : {m.meter_number}  received-register={'yes' if m.has_received else 'no'}"
              f"  intervals {span}")
        days = archive.daily_buckets(m.meter_number)
        if days:
            print(f"          daily history {days[0].day} .. {days[-1].day} ({len(days)} days)")
    print("recent fetches:")
    for f in archive.fetch_history(limit=10):
        status = "ok " if f["ok"] else "ERR"
        extra = f"  {f['error']}" if f.get("error") else ""
        print(f"  #{f['id']:<5} {_fmt_utc(f['fetched_at_utc'])}  {status} {f['kind']:7}{extra}")
    print(f"integrity: {archive.integrity_check()}")
    return 0


def _gaps(archive: Archive, meter: str | None) -> int:
    meter = _single_meter(archive, meter)
    report = archive.gaps(meter)
    if report.first_day is None:
        print(f"{meter}: no interval data archived")
        return 0
    print(f"{meter}: {report.first_day} .. {report.last_publishable_day} "
          f"({len(report.days)} publishable days)")
    for gap in report.incomplete_days:
        state = "recoverable" if gap.recoverable else "PERMANENT"
        labels = ", ".join(gap.missing_labels[:8])
        if len(gap.missing_labels) > 8:
            labels += " ..."
        print(f"  {gap.day}  missing {gap.missing:>3}/{gap.expected}  {state:11}  {labels}")
    print(
        f"missing: {report.recoverable_missing} recoverable, "
        f"{report.permanent_missing} permanent"
    )
    return 1 if report.permanent_missing else 0


def _verify(archive: Archive, meter: str | None) -> int:
    meter = _single_meter(archive, meter)
    issues = archive.consistency_report(meter)
    if not issues:
        print(f"{meter}: consistent (no issues)")
        return 0
    worst = 0
    for issue in issues:
        flag = "WARN" if issue.severity == "warning" else "info"
        worst = max(worst, 1 if issue.severity == "warning" else 0)
        window = ""
        if issue.window_from_utc is not None:
            window = f"  [{_fmt_utc(issue.window_from_utc)} .. {_fmt_utc(issue.window_to_utc)}]"
        print(f"  {flag}  {issue.kind:16} {issue.detail}{window}")
    return worst


def _reparse(archive: Archive, meter: str | None) -> int:
    meter = _single_meter(archive, meter)
    outcomes = reparse(archive, meter=meter)
    if not outcomes:
        print("no retained raw pages to re-parse")
        return 0
    failures = 0
    for o in outcomes:
        if o.store is None:
            failures += 1
            print(f"  page {o.page_id:<5} {o.kind:7} FAILED  {o.error}")
        else:
            print(f"  page {o.page_id:<5} {o.kind:7} +{o.store.inserted} new, "
                  f"{o.store.updated} updated, {o.store.revisions} revisions")
    return 1 if failures else 0


def _export_csv(archive: Archive, meter: str | None, out: str | None) -> int:
    meter = _single_meter(archive, meter)
    readings = archive.intervals(meter)
    handle = open(out, "w", newline="", encoding="utf-8") if out else sys.stdout  # noqa: SIM115
    try:
        writer = csv.writer(handle)
        writer.writerow(["meter", "start_local", "start_utc", "kwh_delivered", "kwh_received",
                         "temperature_f"])
        for r in readings:
            writer.writerow([
                meter, r.start.isoformat(), r.start_utc,
                "" if r.kwh_delivered is None else str(r.kwh_delivered),
                "" if r.kwh_received is None else str(r.kwh_received),
                "" if r.temperature_f is None else str(r.temperature_f),
            ])
    finally:
        if out:
            handle.close()
    if out:
        print(f"wrote {len(readings)} readings to {out}")
    return 0


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="myusage-archive",
        description=(
            "Unofficial MyUsage archiver. Unaffiliated with Exceleron Software or OUC."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--db", help=f"archive path (default: $MYUSAGE_DB or ./{DEFAULT_DB})")
    parser.add_argument(
        "--keep-raw", type=int, default=3, metavar="N",
        help="retain the newest N successfully parsed raw pages per kind (failures are "
        "always kept); default 3",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login-test", help="verify credentials and the landing page")
    p_fetch = sub.add_parser("fetch", help="run one archive cycle (daily table + 15-min grid)")
    p_fetch.add_argument("--meter", help="meter number (default: the single meter on the account)")

    p_backfill = sub.add_parser(
        "backfill", help="one-shot daily-history range fetch for the statistics backfill"
    )
    p_backfill.add_argument(
        "--days", type=int, default=DEFAULT_BACKFILL_DAYS,
        help=f"how far back to ask for (default {DEFAULT_BACKFILL_DAYS}; "
             "the portal keeps ~15 months)",
    )
    p_probe = sub.add_parser("probe", help="M0 probe/capture harness")
    p_probe.add_argument("--out", default="probes", help="output directory (default: probes/)")
    p_probe.add_argument("--scrub", action="append", default=[], metavar="TEXT",
                         help="extra literal string to scrub from the anonymized bundle")
    p_probe.add_argument("--skip-auth-matrix", action="store_true",
                         help="only the canonical login")
    p_probe.add_argument("--grids-only", action="store_true",
                         help="lean repeat capture: one login + the two interval grids")
    p_probe.add_argument("--recheck", action="store_true",
                         help="test whether a previously saved session is still alive")

    for p in (p_login, p_fetch, p_backfill, p_probe):
        p.add_argument("--email", help="account email (or MYUSAGE_EMAIL)")
        p.add_argument("--password", help="account password (prefer MYUSAGE_PASSWORD or prompt)")

    sub.add_parser("status", help="archive stats and recent fetches")
    for name, help_text in (
        ("gaps", "per-day completeness report"),
        ("verify", "cross-check intervals against the daily table"),
        ("reparse", "re-run the parser over retained raw pages"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--meter")
    p_csv = sub.add_parser("export-csv", help="dump 15-minute readings as CSV")
    p_csv.add_argument("--meter")
    p_csv.add_argument("--out", help="file path (default: stdout)")

    args = parser.parse_args(argv)
    level = logging.WARNING - min(args.verbose, 3) * 10
    logging.basicConfig(level=max(level, logging.DEBUG - 1), format="%(levelname)s %(message)s")

    try:
        return _dispatch(args)
    except BrokenPipeError:  # e.g. piped into `head`
        return 0
    except MyUsageError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "login-test":
        email, password = _resolve_credentials(args)
        return asyncio.run(_login_test(email, password))
    if args.command == "fetch":
        email, password = _resolve_credentials(args)
        return asyncio.run(_fetch(email, password, _archive(args), args.meter))
    if args.command == "backfill":
        email, password = _resolve_credentials(args)
        return asyncio.run(_backfill(email, password, _archive(args), args.days))
    if args.command == "probe":
        return _probe(args)
    if args.command == "status":
        return _status(_archive(args))
    if args.command == "gaps":
        return _gaps(_archive(args), args.meter)
    if args.command == "verify":
        return _verify(_archive(args), args.meter)
    if args.command == "reparse":
        return _reparse(_archive(args), args.meter)
    if args.command == "export-csv":
        return _export_csv(_archive(args), args.meter, args.out)
    return 1  # pragma: no cover


def _probe(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if args.recheck:
        email, password = _resolve_credentials(args, required=False)
        report = asyncio.run(ProbeRunner(email, password, out).recheck())
        print(f"recheck written: {report}")
        return 0
    email, password = _resolve_credentials(args)
    runner = ProbeRunner(
        email, password, out, scrub_extra=list(args.scrub),
        skip_auth_matrix=args.skip_auth_matrix, grids_only=args.grids_only,
    )
    report = asyncio.run(runner.run())
    print()
    print(f"report:            {report}")
    print(f"raw captures:      {out / 'raw'}  (git-ignored - contains your real data)")
    print(f"anonymized bundle: {out / 'anonymized'}")
    print()
    print("BEFORE SHARING the anonymized bundle: open the files and eyeball them.")
    print("The leak scanner catches emails, tokens, meters and account numbers, but")
    print("cannot recognize your name or street address unless passed via --scrub.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
