"""Regression tests for identifiers that leaked in the 2026-09-08 live run.

The first probe bundle still contained the account number (rendered under an
"Account #" heading) and the internal numeric MeterID (used in form options
and query strings). Both must be scrubbed automatically.
"""

from __future__ import annotations

from myusage_archive.anonymize import Anonymizer
from myusage_archive.probe import scan_for_leaks

LIVE_SHAPED_PAGE = """
<html><body>
  <div class="box"><h3>Account #</h3><h2>4274623663</h2><span>Balance</span></div>
  <select id="MeterID" name="MeterID">
    <option value="1086144" selected>7CD06051</option>
  </select>
  <a href="/data.cfm?appTransition=View+Hourly+Usage&Service=Electric&MeterID=1086144&EndID=14">
    Hourly
  </a>
  <table id="gridUsageHistory">
    <tr><th>Meter</th><th>kWh Del</th></tr>
    <tr><td>7CD06051</td><td>56</td></tr>
  </table>
</body></html>
"""


def _anonymized() -> tuple[str, Anonymizer]:
    anon = Anonymizer(email="real.person@example.net")
    anon.learn_meters_from_html(LIVE_SHAPED_PAGE)
    return anon.apply(LIVE_SHAPED_PAGE), anon


def test_account_number_is_scrubbed() -> None:
    out, anon = _anonymized()
    assert "4274623663" not in out
    assert anon.mapping["4274623663"].startswith("ACCT")


def test_internal_meter_id_is_scrubbed_in_options_and_urls() -> None:
    out, anon = _anonymized()
    assert "1086144" not in out
    assert anon.mapping["1086144"].startswith("ID")
    # The navigation index is not an identifier and must survive.
    assert "EndID=14" in out


def test_display_meter_still_mapped() -> None:
    out, anon = _anonymized()
    assert "7CD06051" not in out
    assert anon.mapping["7CD06051"] == "MTR001"


def test_mapping_is_deterministic_across_runs() -> None:
    first, _ = _anonymized()
    second, _ = _anonymized()
    assert first == second


def test_leak_scanner_flags_unscrubbed_identifiers() -> None:
    leaks = scan_for_leaks({"page.html": LIVE_SHAPED_PAGE}, email="real.person@example.net")
    kinds = {leak["kind"] for leak in leaks}
    assert "account_label" in kinds
    assert "id_param" in kinds


def test_leak_scanner_clean_on_anonymized_output() -> None:
    out, _ = _anonymized()
    assert scan_for_leaks({"page.html": out}, email="real.person@example.net") == []


def test_leak_scanner_ignores_timestamps() -> None:
    text = 'landing appFlow=20260908082212524 and timeval 1788855809'
    assert scan_for_leaks({"p.html": text}) == []
