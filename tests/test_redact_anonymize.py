"""Redaction and anonymization: no secret may survive."""

from conftest import make_daily_history

from myusage_archive.anonymize import Anonymizer
from myusage_archive.redact import scrub_text, scrub_url


def test_scrub_url_hides_sso_token() -> None:
    url = (
        "https://www.myusage.com/default.cfm?requestAction=Login"
        "&LoginEmail=WebSSOLogin&LoginPassword=12345678901234567890123456789012"
    )
    out = scrub_url(url)
    assert "12345678901234567890123456789012" not in out
    assert "requestAction=Login" in out


def test_scrub_text_patterns() -> None:
    text = (
        "cookie CFID=1234567; CFTOKEN=98765432 and token "
        "abcdefabcdefabcdefabcdefabcdefab plus LoginPassword=deadbeef"
    )
    out = scrub_text(text)
    assert "1234567" not in out
    assert "98765432" not in out
    assert "abcdefabcdefabcdefabcdefabcdefab" not in out
    assert "deadbeef" not in out


def test_scrub_text_extra_secrets() -> None:
    out = scrub_text("password is hunter2 ok", extra_secrets=["hunter2"])
    assert "hunter2" not in out


def test_anonymizer_learns_meters_and_is_deterministic() -> None:
    html = make_daily_history(
        [
            {"meter": "7CD06051", "from": "09/06/2026 01:36 AM", "type": "Valid"},
            {"meter": "7CD06051", "from": "09/05/2026 01:36 AM", "type": "Valid"},
            {"meter": "9XY99999", "from": "09/05/2026 01:36 AM", "type": "Valid"},
        ]
    )
    anon = Anonymizer(email="john.q@example.net")
    found = anon.learn_meters_from_html(html)
    assert "7CD06051" in found and "9XY99999" in found
    out1 = anon.apply(html)
    out2 = anon.apply(html)
    assert out1 == out2
    assert "7CD06051" not in out1
    assert "9XY99999" not in out1
    assert "MTR001" in out1


def test_anonymizer_scrubs_email_and_extras() -> None:
    anon = Anonymizer(email="jane.doe@example.net", extra_values=["123 Palm Ave"])
    out = anon.apply("jane.doe@example.net lives at 123 Palm Ave (jane.doe)")
    assert "jane.doe" not in out
    assert "123 Palm Ave" not in out
    assert "user@example.com" in out


def test_anonymizer_scrubs_account_holder_name_by_structure() -> None:
    """The portal header carries the customer's name above the account line.
    It must be replaced without the operator having to know to pass it."""
    html = (
        '<div id="account-navigation"><div class="details">\n'
        "<b>Public,John Q</b><br /><br />\n Account: 12345678\n</div></div>"
        '<table id="gridUsageHistory"><tr><td>7CD06051</td></tr></table>'
    )
    anon = Anonymizer()
    found = anon.learn_meters_from_html(html)
    assert "Public,John Q" in found
    out = anon.apply(html)
    assert "Public" not in out and "John" not in out
    assert "<b>Customer,Sample</b>" in out
    assert "12345678" not in out


def test_live_fixtures_carry_no_account_holder_name() -> None:
    from pathlib import Path

    from myusage_archive.probe import scan_for_leaks

    fixtures = Path(__file__).parent / "fixtures" / "live"
    files = {p.name: p.read_text(encoding="utf-8") for p in fixtures.glob("*.html")}
    assert files
    for text in files.values():
        assert "<b>Customer,Sample</b>" in text
    assert [f for f in scan_for_leaks(files) if f["kind"] == "holder_name"] == []


def test_leak_scanner_flags_a_surviving_holder_name() -> None:
    from myusage_archive.probe import scan_for_leaks

    page = '<div class="details"><b>Doe,Jane</b><br /><br /> Account: ACCT001 </div>'
    kinds = {f["kind"] for f in scan_for_leaks({"p.html": page})}
    assert "holder_name" in kinds
