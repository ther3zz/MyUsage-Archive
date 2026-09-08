"""Deterministic fixture anonymizer.

Captured pages become shareable test fixtures only after every account
identifier is rewritten. Replacements are deterministic within a bundle
(the same value always maps to the same placeholder) so cross-page
relationships survive anonymization.

Covered automatically:
- the login email (and its local part standing alone),
- display meter numbers from the first cell of usage-grid rows (e.g. 7CD06051),
- internal numeric ids from MeterID/AccountID form fields and URLs,
- the account number shown under an "Account #" / "Account Number" label,
- the account holder's name in the page header (the bold line above
  "Account:" inside the ``details`` block),
- SSO tokens, CFID/CFTOKEN, xsrf_token/asid values (via redact patterns),
- any extra strings the operator passes (names, street address, ...).

The anonymizer cannot recognize PII it has never seen — the operator must
still eyeball the bundle before sharing it. The CLI prints that reminder,
and ProbeRunner scans the finished bundle for anything that still looks like
an identifier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag

from .redact import scrub_text

# Form-field / URL parameter names that carry an account-linked numeric id.
# Small navigation indices (EndID, StartID, chart offsets) are deliberately
# excluded — they are not identifiers.
_ID_FIELD_NAMES = {"meterid", "accountid", "premiseid", "serviceid", "customerid"}

# Labels that precede an account number in the page chrome.
_ACCOUNT_LABEL_RE = re.compile(r"account\s*(?:#|number|no\.?)?", re.IGNORECASE)

# The account holder's name as the portal renders it in the header:
#   <div class="details"> <b>Last,First</b><br /><br /> Account: NNN </div>
# Learned by structure, not by value, so it is scrubbed even when the operator
# never passed it via --scrub (the 2026-09-08 bundle leaked it this way).
_HOLDER_NAME_PLACEHOLDER = "Customer,Sample"


@dataclass
class Anonymizer:
    """Stateful, deterministic replacer shared across one capture bundle."""

    email: str | None = None
    extra_values: list[str] = field(default_factory=list)
    _mapping: dict[str, str] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def _placeholder(self, value: str, prefix: str) -> str:
        if value not in self._mapping:
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            self._mapping[value] = f"{prefix}{self._counters[prefix]:03d}"
        return self._mapping[value]

    def learn_meters_from_html(self, html: str) -> list[str]:
        """Harvest identifiers from one page: display meters, internal ids, account #."""
        soup = BeautifulSoup(html, "html.parser")
        found: list[str] = []

        # 1. Display meter numbers: first cell of usage-grid rows (e.g. 7CD06051).
        for table in soup.find_all("table"):
            if not isinstance(table, Tag):
                continue
            if not str(table.get("id") or "").lower().startswith("grid"):
                continue
            for row in table.find_all("tr"):
                if not isinstance(row, Tag):
                    continue
                cell = row.find("td")
                if not isinstance(cell, Tag):
                    continue
                text = cell.get_text(strip=True)
                if re.fullmatch(r"[0-9A-Za-z-]{5,16}", text) and any(c.isdigit() for c in text):
                    self._placeholder(text, "MTR")
                    found.append(text)

        # 2. Internal numeric ids from form controls and hidden inputs.
        for tag in soup.find_all(("input", "select", "option")):
            if not isinstance(tag, Tag):
                continue
            name = str(tag.get("name") or "").lower()
            if name in _ID_FIELD_NAMES:
                for value in self._values_under(tag):
                    if value.isdigit():
                        self._placeholder(value, "ID")
                        found.append(value)

        # 3. Internal ids embedded in query strings anywhere in the markup
        #    (e.g. ...&MeterID=1086144&...). Covers <a href> and inline JS.
        for field_name in _ID_FIELD_NAMES:
            for match in re.finditer(
                rf"{field_name}=(\d{{4,}})", html, re.IGNORECASE
            ):
                self._placeholder(match.group(1), "ID")
                found.append(match.group(1))

        # 4. Account number under an "Account #"/"Account Number" label.
        for label in soup.find_all(string=_ACCOUNT_LABEL_RE):
            digits = self._nearby_digits(label)
            if digits:
                self._placeholder(digits, "ACCT")
                found.append(digits)

        # 5. Account holder's name: the bold text inside the header details
        #    block that also carries the "Account:" line.
        for details in soup.find_all("div", class_="details"):
            if not isinstance(details, Tag):
                continue
            if not _ACCOUNT_LABEL_RE.search(details.get_text(" ", strip=True)):
                continue
            for bold in details.find_all("b"):
                if not isinstance(bold, Tag):
                    continue
                name = bold.get_text(strip=True)
                if len(name) >= 3 and not name.isdigit() and name != _HOLDER_NAME_PLACEHOLDER:
                    self._mapping.setdefault(name, _HOLDER_NAME_PLACEHOLDER)
                    found.append(name)

        return found

    @staticmethod
    def _values_under(tag: Tag) -> list[str]:
        values = []
        raw = tag.get("value")
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
        for option in tag.find_all("option"):
            if isinstance(option, Tag):
                ov = option.get("value")
                if isinstance(ov, str) and ov.strip():
                    values.append(ov.strip())
        return values

    @staticmethod
    def _nearby_digits(label_node: object) -> str | None:
        """Find a 4+ digit run near an account label (same or adjacent element)."""
        parent = getattr(label_node, "parent", None)
        candidates: list[str] = []
        # The value often sits in the next sibling element (e.g. <h3>Account #</h3><h2>NNN</h2>).
        for sib in list(getattr(parent, "next_siblings", [])) [:3]:
            if isinstance(sib, Tag):
                candidates.append(sib.get_text(" ", strip=True))
        # Or inside the same container just after the label.
        grandparent = getattr(parent, "parent", None)
        if isinstance(grandparent, Tag):
            candidates.append(grandparent.get_text(" ", strip=True))
        for text in candidates:
            match = re.search(r"\b(\d{4,})\b", text)
            if match:
                return match.group(1)
        return None

    def apply(self, text: str) -> str:
        """Rewrite all learned and configured identifiers in *text*."""
        out = text
        # Longest-first so overlapping values (e.g. full email vs local part,
        # or an id that is a substring of another) rewrite cleanly.
        for value, placeholder in sorted(self._mapping.items(), key=lambda kv: -len(kv[0])):
            out = out.replace(value, placeholder)
        if self.email:
            local = self.email.split("@", 1)[0]
            out = out.replace(self.email, "user@example.com")
            if len(local) >= 3:
                out = out.replace(local, "user")
        for value in sorted(set(self.extra_values), key=len, reverse=True):
            if value:
                out = out.replace(value, "SCRUBBED")
        return scrub_text(out)

    @property
    def mapping(self) -> dict[str, str]:
        return dict(self._mapping)
