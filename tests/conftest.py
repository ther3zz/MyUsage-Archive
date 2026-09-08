"""Shared synthetic-fixture builders.

These synthesize pages that follow the structures confirmed against a live
OUC solar account on 2026-09-07. They are placeholders until M0's real
anonymized captures land; tests that use them are layout tests, not proof of
portal behavior.
"""

from __future__ import annotations


def make_interval_grid(
    table_id: str,
    dates: list[str],
    weekdays: list[str],
    metrics: list[str],
    time_labels: list[str],
    values: dict[str, list[list[str]]],
    trailing: list[tuple[str, list[str]]] | None = None,
) -> str:
    """Build an interval grid the way the portal renders one.

    values[date] is a list per time-label of per-metric cell strings.
    """
    day_count = len(dates)
    head = "<tr><td>&nbsp;</td>" + "".join(f"<td>{d}</td>" for d in dates) + "</tr>"
    week = "<tr><td>&nbsp;</td>" + "".join(f"<td>{w}</td>" for w in weekdays) + "</tr>"
    chart = "<tr><td>&nbsp;</td>" + "<td>Chart</td>" * day_count + "</tr>"
    label0 = "Time" if "15" in table_id else "Hour"
    metric_row = (
        f"<tr><td>{label0}</td>"
        + "".join("".join(f"<td>{m}</td>" for m in metrics) for _ in dates)
        + "</tr>"
    )
    body_rows = []
    for i, label in enumerate(time_labels):
        cells = "".join(
            "".join(f'<td data-raw-value="{v}">{v}</td>' for v in values[d][i]) for d in dates
        )
        body_rows.append(f"<tr><td>{label}</td>{cells}</tr>")
    for label, cells in trailing or []:
        row = "".join(f"<td>{c}</td>" for c in cells)
        body_rows.append(f"<tr><td>{label}</td>{row}</tr>")
    return (
        f'<html><body><table id="{table_id}">'
        + head
        + week
        + chart
        + metric_row
        + "".join(body_rows)
        + "</table></body></html>"
    )


def make_daily_history(
    rows: list[dict[str, str]],
    solar: bool = True,
    with_form: bool = True,
) -> str:
    """Build a gridUsageHistory page (11-col solar / 10-col non-solar)."""
    if solar:
        headers = [
            "Meter", "High", "Low", "Posted", "From", "To",
            "kWh Delivered", "kWh Received", "kW", "Reading", "Type",
        ]
        keys = [
            "meter", "high", "low", "posted", "from", "to",
            "kwh_del", "kwh_rcvd", "kw", "reading", "type",
        ]
    else:
        headers = [
            "Meter", "High", "Low", "Posted", "From", "To", "kWh", "Reading", "Type",
        ]
        keys = ["meter", "high", "low", "posted", "from", "to", "kwh", "reading", "type"]
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
    body = "".join(
        "<tr>" + "".join(f"<td>{r.get(k, '')}</td>" for k in keys) + "</tr>" for r in rows
    )
    form = (
        '<form action="/data.cfm" method="post">'
        '<input type="hidden" name="cf_CSRFToken" value="AAAA1111">'
        '<input type="hidden" name="cf_CSRFToken_web" value="BBBB2222">'
        '<input name="FromDate" value=""><input name="ToDate" value="">'
        '<select name="ServiceType"><option selected value="Electric">Electric</option></select>'
        "</form>"
        if with_form
        else ""
    )
    return (
        "<html><body>"
        + form
        + f'<table id="gridUsageHistory">{head}{body}</table>'
        + "</body></html>"
    )
