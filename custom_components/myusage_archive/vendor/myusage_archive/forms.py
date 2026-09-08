"""Whole-form scraping (ADR-0004 idiom).

Home Assistant's web-scraping ADR amendment permits scraping the
authentication phase but demands fields be gathered *all at once* rather than
by per-field regexes. This module parses every ``<form>`` on a page into a
structured object; callers pick the form they need and read its inputs as a
dict. No ``re.search(...).group(1)`` anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag

from .exceptions import LayoutError


@dataclass(frozen=True)
class Form:
    """One HTML form: its action, method, and every input's current value."""

    action: str
    method: str
    form_id: str
    inputs: dict[str, str] = field(default_factory=dict)


def parse_forms(html: str) -> list[Form]:
    """Extract every form with all of its input/select/textarea fields."""
    soup = BeautifulSoup(html, "html.parser")
    forms: list[Form] = []
    for form_tag in soup.find_all("form"):
        if not isinstance(form_tag, Tag):
            continue
        inputs: dict[str, str] = {}
        for inp in form_tag.find_all(("input", "select", "textarea")):
            if not isinstance(inp, Tag):
                continue
            name = inp.get("name")
            if not name or not isinstance(name, str):
                continue
            itype = str(inp.get("type") or "").lower()
            if itype in {"button", "reset", "image", "file"}:
                continue
            if itype in {"checkbox", "radio"} and inp.get("checked") is None:
                continue
            if inp.name == "select":
                selected = inp.find("option", attrs={"selected": True})
                value = selected.get("value", "") if isinstance(selected, Tag) else ""
            elif inp.name == "textarea":
                value = inp.get_text()
            else:
                value = inp.get("value") or ""
            inputs[name] = value if isinstance(value, str) else ""
        forms.append(
            Form(
                action=str(form_tag.get("action") or ""),
                method=str(form_tag.get("method") or "get").lower(),
                form_id=str(form_tag.get("id") or ""),
                inputs=inputs,
            )
        )
    return forms


def find_form_with_fields(html: str, required_fields: tuple[str, ...]) -> Form:
    """Return the first form carrying every one of *required_fields*.

    Raises LayoutError naming the missing fields — never a bare AttributeError.
    """
    forms = parse_forms(html)
    for form in forms:
        if all(name in form.inputs for name in required_fields):
            return form
    available = [sorted(f.inputs) for f in forms]
    raise LayoutError(
        f"No form contains the required fields {sorted(required_fields)}; "
        f"forms present (by field names): {available!r}"
    )
