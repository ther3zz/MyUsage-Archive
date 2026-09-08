"""Probe analysis heuristics against synthetic grids built to the confirmed layout."""

from __future__ import annotations

from conftest import make_daily_history, make_interval_grid

from myusage_archive.probe import (
    _daily_table_dates,
    analyze_p1_received_midday,
    analyze_p2_trailing_rows,
    analyze_p5_services_meters,
    analyze_p10_alignment,
    grid_to_matrix,
    layout_signature,
    read_grid,
)

DATES = ["09/05", "09/04"]
WEEKDAYS = ["Sat", "Fri"]
METRICS_15 = ["°F", "kWh Delivered", "kWh Received", "kW"]
METRICS_H = ["°F", "kWh Delivered", "kWh Received"]


def build_15min() -> str:
    labels = ["12:00 AM", "12:15 AM", "12:30 AM", "12:45 AM", "1:00 AM", "12:00 PM", "12:15 PM"]
    values = {
        # temp, kwh_del, kwh_rcvd, kw per row
        "09/05": [
            ["76", "0.400", "0.000", "1.600"],
            ["76", "0.300", "0.000", "1.200"],
            ["75", "0.200", "0.000", "0.800"],
            ["75", "0.100", "0.000", "0.400"],
            ["75", "0.500", "0.000", "2.000"],
            ["92", "0.050", "1.250", "0.200"],  # midday: solar export non-zero
            ["93", "0.040", "1.300", "0.160"],
        ],
        "09/04": [
            ["77", "0.500", "0.000", "2.000"],
            ["77", "0.400", "0.000", "1.600"],
            ["76", "0.300", "0.000", "1.200"],
            ["76", "0.200", "0.000", "0.800"],
            ["76", "0.600", "0.000", "2.400"],
            ["91", "0.000", "0.900", "0.000"],
            ["91", "0.010", "0.950", "0.040"],
        ],
    }
    trailing = [
        ("Total", ["", "1.590", "2.550", ""] * 2),
        ("Peak", ["", "0.500", "1.300", ""] * 2),
    ]
    return make_interval_grid(
        "grid15MinuteUsage", DATES, WEEKDAYS, METRICS_15, labels, values, trailing
    )


def build_hourly() -> str:
    labels = ["12:00 AM - 01:00 AM", "01:00 AM - 02:00 AM"]
    values = {
        # 12-1 bucket equals the sum of the four rows LABELED 12:00..12:45
        # (start-labeled hypothesis): 0.4+0.3+0.2+0.1 = 1.000
        "09/05": [["76", "1.000", "0.000"], ["75", "0.700", "0.000"]],
        "09/04": [["77", "1.400", "0.000"], ["76", "0.900", "0.000"]],
    }
    trailing = [("Total", ["", "1.700", "0.000"] * 2)]
    return make_interval_grid(
        "gridHourlyUsage", DATES, WEEKDAYS, METRICS_H, labels, values, trailing
    )


def test_read_grid_measures_stride_without_temperature_anchor() -> None:
    matrix = grid_to_matrix(build_15min(), "grid15MinuteUsage")
    assert matrix is not None
    view = read_grid(matrix, "grid15MinuteUsage")
    assert view.dates == DATES
    assert view.stride == 4
    assert view.metrics == ["temp_f", "kwh_delivered", "kwh_received", "kw"]
    assert len(view.data_rows) == 7
    assert len(view.trailing_rows) == 2
    assert view.anomalies == []


def test_abbreviated_live_headers_canonicalize() -> None:
    """The live portal renders 'kWh Del' / 'kWh Rcvd' (observed 2026-09-08)."""
    html = make_interval_grid(
        "grid15MinuteUsage",
        DATES,
        WEEKDAYS,
        ["°F", "kWh Del", "kWh Rcvd", "kW"],
        ["12:00 AM"],
        {"09/05": [["76", "0.4", "0.0", "1.6"]], "09/04": [["77", "0.5", "0.0", "2.0"]]},
    )
    matrix = grid_to_matrix(html, "grid15MinuteUsage")
    assert matrix is not None
    view = read_grid(matrix, "grid15MinuteUsage")
    assert view.metrics == ["temp_f", "kwh_delivered", "kwh_received", "kw"]
    assert view.anomalies == []


def test_read_grid_without_temperature_column() -> None:
    html = make_interval_grid(
        "gridHourlyUsage",
        DATES,
        WEEKDAYS,
        ["kWh"],
        ["12:00 AM - 01:00 AM"],
        {"09/05": [["1.0"]], "09/04": [["2.0"]]},
    )
    matrix = grid_to_matrix(html, "gridHourlyUsage")
    assert matrix is not None
    view = read_grid(matrix, "gridHourlyUsage")
    assert view.stride == 1
    assert view.metrics == ["kwh"]


def test_p1_detects_midday_export() -> None:
    matrix = grid_to_matrix(build_15min(), "grid15MinuteUsage")
    assert matrix is not None
    view = read_grid(matrix, "grid15MinuteUsage")
    result = analyze_p1_received_midday(view)
    assert result["answer"].startswith("YES")
    day = result["per_day"][0]
    assert day["nonzero"] == 2


def test_p2_reports_trailing_rows() -> None:
    matrix = grid_to_matrix(build_15min(), "grid15MinuteUsage")
    assert matrix is not None
    view = read_grid(matrix, "grid15MinuteUsage")
    result = analyze_p2_trailing_rows(view)
    assert result["count"] == 2
    assert result["rows"][0]["label"] == "Total"


def test_p10_identifies_start_labels() -> None:
    m15 = grid_to_matrix(build_15min(), "grid15MinuteUsage")
    mh = grid_to_matrix(build_hourly(), "gridHourlyUsage")
    assert m15 is not None and mh is not None
    result = analyze_p10_alignment(
        read_grid(m15, "grid15MinuteUsage"), read_grid(mh, "gridHourlyUsage")
    )
    assert result["answer"] == "labels are interval STARTS"


def test_daily_table_dates_and_services() -> None:
    html = make_daily_history(
        [
            {
                "meter": "7CD06051",
                "from": "09/06/2026 01:36 AM",
                "to": "09/07/2026 01:33 AM",
                "kwh_del": "56",
                "kwh_rcvd": "41",
                "type": "Valid",
            },
            {
                "meter": "7CD06051",
                "from": "08/06/2026 01:36 AM",
                "to": "08/07/2026 01:33 AM",
                "kwh_del": "50",
                "kwh_rcvd": "38",
                "type": "Valid",
            },
        ]
    )
    dates = _daily_table_dates(html)
    assert dates["present"] is True
    assert dates["data_rows"] == 2
    assert dates["earliest"] == "08/06/2026"
    assert dates["latest"] == "09/07/2026"
    services = analyze_p5_services_meters(html)
    assert services["meters_seen"] == ["7CD06051"]
    assert services["service_options"] == ["Electric"]


def test_layout_signature_shape() -> None:
    matrix = grid_to_matrix(build_15min(), "grid15MinuteUsage")
    assert matrix is not None
    view = read_grid(matrix, "grid15MinuteUsage")
    sig = layout_signature(view, len(matrix))
    assert sig["day_count"] == 2
    assert sig["stride"] == 4
    assert sig["trailing_rows"] == 2
