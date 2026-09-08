"""Secret scrubbing for logs, exception messages, and shared artifacts.

The SSO handoff rides a single-use token in a URL query parameter, and the
ColdFusion session lives in CFID/CFTOKEN cookies — so URLs and headers are
treated as sensitive by default. Nothing in this library may log a URL,
cookie, or response snippet without passing it through these helpers.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "REDACTED"

# Query parameters whose values must never appear in logs or reports.
_SENSITIVE_QUERY_PARAMS = {
    "loginpassword",  # the 32-digit single-use SSO token
    "loginemail",
    "password",
    "email",
    "cfid",
    "cftoken",
    "xsrf_token",
    "asid",
}

# Free-text patterns scrubbed from any string headed for a log or report.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # SSO token: 32 hex/digit chars in a LoginPassword param or standalone.
    (re.compile(r"(LoginPassword=)[^&\s\"']+", re.IGNORECASE), r"\1" + REDACTED),
    (re.compile(r"(LoginEmail=)[^&\s\"']+", re.IGNORECASE), r"\1" + REDACTED),
    (re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE), REDACTED),
    # ColdFusion session cookies wherever they surface.
    (re.compile(r"(CFID=)[^;,\s\"']+", re.IGNORECASE), r"\1" + REDACTED),
    (re.compile(r"(CFTOKEN=)[^;,\s\"']+", re.IGNORECASE), r"\1" + REDACTED),
    (re.compile(r"(Set-Cookie:)[^\n]*", re.IGNORECASE), r"\1 " + REDACTED),
]


def scrub_url(url: str) -> str:
    """Return *url* with sensitive query parameter values replaced."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    cleaned = [
        (k, REDACTED if k.lower() in _SENSITIVE_QUERY_PARAMS else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(cleaned)))


def scrub_text(text: str, extra_secrets: list[str] | None = None) -> str:
    """Scrub known secret patterns (and any *extra_secrets* verbatim) from text."""
    out = text
    for secret in extra_secrets or []:
        if secret:
            out = out.replace(secret, REDACTED)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out
