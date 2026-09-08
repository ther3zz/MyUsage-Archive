"""Typed exception taxonomy for myusage-archive.

Every failure mode gets a distinct type so callers (CLI, Home Assistant
integration) can react precisely. Parsers and the client must raise — never
substitute defaults, never return 0.0 for something they could not read.
"""

from __future__ import annotations


class MyUsageError(Exception):
    """Base class for all myusage-archive errors."""


class TransportError(MyUsageError):
    """Network-level failure: DNS, TLS, timeout, or an unexpected HTTP status."""

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.url is not None:
            parts.append(f"URL: {self.url}")
        if self.status is not None:
            parts.append(f"Status: {self.status}")
        return " | ".join(parts)


class AuthenticationError(MyUsageError):
    """Login rejected: the portal's login JSON carried no redirect_url."""


class SessionExpiredError(MyUsageError):
    """A previously working session or appFlow token stopped being honored."""


class MfaRequiredError(MyUsageError):
    """Reserved: the portal demanded a second factor (none observed as of 2026-09)."""


class UnsupportedAccountError(MyUsageError):
    """Login worked but did not land on the Postpaid app.

    Covers the multi-utility "Select Utility" interstitial and prepay-only
    accounts. Carries a short description of what was seen instead.
    """


class LayoutError(MyUsageError):
    """The page structure does not match any layout this version understands.

    Raised loudly instead of guessing. ``detail`` names exactly what differed.
    """

    def __init__(self, detail: str, *, raw_page_id: int | None = None) -> None:
        super().__init__(detail)
        self.raw_page_id = raw_page_id


class DataError(MyUsageError):
    """A cell value could not be parsed; carries row/column context."""


class ArchiveVersionError(MyUsageError):
    """The SQLite archive schema is newer than this code understands."""


class BlockingCallError(MyUsageError):
    """The archive was touched from a thread running an asyncio event loop.

    Home Assistant's blocking-call detector does not cover sqlite3, so an
    accidental on-loop query would stall HA silently. This guard turns that
    into an immediate, obvious failure. Route archive calls through
    ``hass.async_add_executor_job`` (or any worker thread).
    """
