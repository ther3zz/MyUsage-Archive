"""Whole-form scraping (ADR-0004 idiom)."""

import pytest
from conftest import make_daily_history

from myusage_archive.exceptions import LayoutError
from myusage_archive.forms import find_form_with_fields, parse_forms


def test_parse_forms_reads_all_fields() -> None:
    html = make_daily_history([], with_form=True)
    forms = parse_forms(html)
    assert len(forms) == 1
    form = forms[0]
    assert form.inputs["cf_CSRFToken"] == "AAAA1111"
    assert form.inputs["cf_CSRFToken_web"] == "BBBB2222"
    assert form.inputs["ServiceType"] == "Electric"


def test_find_form_with_fields_raises_layout_error() -> None:
    html = make_daily_history([], with_form=False)
    with pytest.raises(LayoutError) as excinfo:
        find_form_with_fields(html, ("cf_CSRFToken", "cf_CSRFToken_web"))
    assert "cf_CSRFToken" in str(excinfo.value)


def test_unchecked_checkbox_and_buttons_skipped() -> None:
    html = (
        '<form id="f"><input type="checkbox" name="a" value="1">'
        '<input type="checkbox" name="b" value="2" checked>'
        '<input type="button" name="c" value="x">'
        '<input type="hidden" name="d" value="y"></form>'
    )
    form = parse_forms(html)[0]
    assert "a" not in form.inputs
    assert form.inputs["b"] == "2"
    assert "c" not in form.inputs
    assert form.inputs["d"] == "y"
