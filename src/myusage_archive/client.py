"""Async client for the Exceleron MyUsage portal (postpaid surface).

Flow (verified live 2026-09-07, cross-checked against dstamen/myusage-ha):

1. ``GET /`` primes ColdFusion session cookies (CFID/CFTOKEN).
2. ``POST /login`` with ``email``/``password`` as an XHR (JSON Accept,
   ``X-Requested-With``); success returns JSON with a ``redirect_url``.
   Failure is signalled by the *absence* of ``redirect_url``, not by status.
3. Following ``redirect_url`` (a single-use SSO token in the query string)
   lands on ``data.cfm?appPage=Postpaid&...&appFlow=<token>``; ``appFlow``
   must be threaded onto every later request.

This client authenticates directly at myusage.com only. It never touches
ouc.com (whose site terms prohibit automated tools).

The session is injected and never mutated (headers go per-request), so a
caller such as Home Assistant can share its client session.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

import aiohttp

from .const import (
    BASE_URL,
    CSRF_FIELDS,
    DATA_URL,
    HISTORY_QUERY,
    HTML_HEADERS,
    LOGIN_URL,
    LOGIN_XHR_HEADERS,
    REQUEST_TIMEOUT_S,
    SUBMIT_VALIDATE_TIME_PERIOD,
    TRANSITION_15MIN,
    TRANSITION_HOURLY,
)
from .exceptions import (
    AuthenticationError,
    SessionExpiredError,
    TransportError,
    UnsupportedAccountError,
)
from .forms import find_form_with_fields
from .redact import scrub_text, scrub_url

_LOGGER = logging.getLogger(__name__)
_BODY_LOG_LEVEL = logging.DEBUG - 1  # opower convention: -vv shows bodies


class LoginMode(enum.Enum):
    """Variants used by the M0 auth matrix to isolate the load-bearing step."""

    CANONICAL = "prime+xhr"
    PRIME_ONLY = "prime, plain POST"
    XHR_ONLY = "no prime, xhr POST"


@dataclass
class LoginAttempt:
    """Everything observed during one login attempt. Secrets pre-redacted."""

    mode: str
    primed: bool = False
    prime_status: int | None = None
    post_status: int | None = None
    json_keys: list[str] = field(default_factory=list)
    redirect_url_present: bool = False
    landing_url: str | None = None  # redacted
    app_flow_found: bool = False
    app_page: str | None = None
    ok: bool = False
    error: str | None = None  # redacted
    response_excerpt: str | None = None  # scrubbed failure payload, for diagnosis
    failure: str | None = None  # machine-readable kind: auth_rejected | interstitial |
    #                             sso_bounce | no_app_flow | not_json | transport


def extract_app_flow(url: str) -> str | None:
    """Pull the appFlow token out of a landing URL, if present."""
    query = parse_qs(urlsplit(url).query)
    values = query.get("appFlow") or query.get("appflow")
    return values[0] if values else None


def looks_like_login_page(html: str) -> bool:
    """Heuristic: the page is the public homepage/login, not the app."""
    lowered = html.lower()
    return 'name="password"' in lowered and 'name="email"' in lowered


def looks_like_utility_select(html: str) -> bool:
    """Heuristic for the multi-utility interstitial seen in the login frame."""
    lowered = html.lower()
    return "select utility" in lowered or "select_utility" in lowered


class MyUsageClient:
    """Authenticated access to one MyUsage postpaid account."""

    def __init__(self, email: str, password: str, session: aiohttp.ClientSession) -> None:
        self._email = email
        self._password = password
        self._session = session
        self._app_flow: str | None = None
        self._landing_html: str | None = None
        self._timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)

    # ------------------------------------------------------------------ helpers

    @property
    def app_flow(self) -> str | None:
        return self._app_flow

    @property
    def landing_html(self) -> str | None:
        """The post-login landing page (evidence for P5/P11 probes)."""
        return self._landing_html

    def _secrets(self) -> list[str]:
        return [self._password, self._email]

    async def _get(self, url: str, headers: dict[str, str] | None = None) -> tuple[str, str, int]:
        """GET returning (body, final_url, status); raises TransportError."""
        _LOGGER.debug("GET %s", scrub_url(url))
        try:
            async with self._session.get(
                url, headers=headers or HTML_HEADERS, timeout=self._timeout
            ) as resp:
                body = await resp.text()
                final_url = str(resp.url)
                _LOGGER.log(_BODY_LOG_LEVEL, "GET %s -> %s bytes", scrub_url(url), len(body))
                return body, final_url, resp.status
        except aiohttp.ClientError as err:
            raise TransportError(
                f"GET failed: {scrub_text(str(err), self._secrets())}", url=scrub_url(url)
            ) from err

    async def _post(
        self, url: str, data: dict[str, str], headers: dict[str, str]
    ) -> tuple[str, str, int]:
        """POST returning (body, final_url, status); raises TransportError."""
        _LOGGER.debug("POST %s", scrub_url(url))
        try:
            async with self._session.post(
                url, data=data, headers=headers, timeout=self._timeout
            ) as resp:
                body = await resp.text()
                final_url = str(resp.url)
                _LOGGER.log(_BODY_LOG_LEVEL, "POST %s -> %s bytes", scrub_url(url), len(body))
                return body, final_url, resp.status
        except aiohttp.ClientError as err:
            raise TransportError(
                f"POST failed: {scrub_text(str(err), self._secrets())}", url=scrub_url(url)
            ) from err

    # -------------------------------------------------------------------- login

    async def attempt_login(self, mode: LoginMode = LoginMode.CANONICAL) -> LoginAttempt:
        """Run one login attempt and record every observable, never raising.

        Used by the M0 auth matrix. ``login()`` wraps this and raises typed
        errors for normal operation.
        """
        attempt = LoginAttempt(mode=mode.value)
        try:
            if mode in (LoginMode.CANONICAL, LoginMode.PRIME_ONLY):
                _, _, status = await self._get(BASE_URL + "/")
                attempt.primed = True
                attempt.prime_status = status

            if mode is LoginMode.PRIME_ONLY:
                headers = dict(HTML_HEADERS)
                headers["Referer"] = BASE_URL + "/"
            else:
                headers = dict(LOGIN_XHR_HEADERS)

            body, _, post_status = await self._post(
                LOGIN_URL, {"email": self._email, "password": self._password}, headers
            )
            attempt.post_status = post_status

            try:
                payload = json.loads(body)
            except ValueError:
                attempt.error = (
                    f"login response was not JSON ({len(body)} bytes, status {post_status})"
                )
                attempt.response_excerpt = scrub_text(body[:300], self._secrets())
                attempt.failure = "not_json"
                return attempt

            if not isinstance(payload, dict):
                attempt.error = f"login JSON was a {type(payload).__name__}, not an object"
                attempt.failure = "not_json"
                return attempt
            attempt.json_keys = sorted(payload)

            redirect_url = payload.get("redirect_url")
            attempt.redirect_url_present = bool(redirect_url)
            if not redirect_url:
                # Observed failure shape (2026-09-08): status 500 with keys
                # {error_str, result}. Keep the whole (scrubbed) payload so a
                # failure report is diagnosable without a re-run.
                attempt.response_excerpt = scrub_text(
                    json.dumps(payload, default=str)[:400], self._secrets()
                )
                message = (
                    payload.get("error_str") or payload.get("data") or payload.get("message")
                )
                shown = (
                    scrub_text(str(message), self._secrets())[:200]
                    if message
                    else attempt.response_excerpt
                )
                attempt.error = f"login rejected (status {post_status}): {shown}"
                attempt.failure = "auth_rejected"
                return attempt

            landing, final_url, _ = await self._get(
                str(redirect_url), {**HTML_HEADERS, "Referer": BASE_URL + "/"}
            )
            attempt.landing_url = scrub_url(final_url)
            app_flow = extract_app_flow(final_url)
            attempt.app_flow_found = app_flow is not None
            query = parse_qs(urlsplit(final_url).query)
            app_pages = query.get("appPage") or []
            attempt.app_page = app_pages[0] if app_pages else None

            if app_flow is None:
                if looks_like_utility_select(landing):
                    attempt.error = (
                        "landed on a Select Utility interstitial (multi-utility account)"
                    )
                    attempt.failure = "interstitial"
                elif looks_like_login_page(landing):
                    attempt.error = "SSO handoff bounced back to the login page"
                    attempt.failure = "sso_bounce"
                else:
                    attempt.error = "no appFlow in the landing URL"
                    attempt.failure = "no_app_flow"
                return attempt

            self._app_flow = app_flow
            self._landing_html = landing
            attempt.ok = True
            return attempt
        except TransportError as err:
            attempt.error = str(err)
            attempt.failure = "transport"
            return attempt

    async def login(self) -> None:
        """Authenticate; on success ``app_flow`` is set.

        :raises AuthenticationError: credentials rejected
        :raises UnsupportedAccountError: multi-utility interstitial / non-Postpaid landing
        :raises TransportError: network or unexpected HTTP failure
        """
        attempt = await self.attempt_login(LoginMode.CANONICAL)
        if attempt.ok:
            if attempt.app_page and attempt.app_page != "Postpaid":
                raise UnsupportedAccountError(
                    f"login landed on appPage={attempt.app_page!r}, not the Postpaid app"
                )
            return
        error = attempt.error or "login failed"
        if attempt.failure == "auth_rejected":
            raise AuthenticationError(error)
        if attempt.failure == "interstitial":
            raise UnsupportedAccountError(error)
        raise TransportError(error)

    # ------------------------------------------------------------------ fetches

    def _history_base(self) -> str:
        if self._app_flow is None:
            raise SessionExpiredError("not logged in (no appFlow)")
        # Byte-faithful to the proven request shape: the screen-sub uses %20.
        return (
            f"{DATA_URL}?appPage={HISTORY_QUERY['appPage']}"
            f"&appPageScreen={HISTORY_QUERY['appPageScreen']}"
            f"&appPageScreenSub={quote(HISTORY_QUERY['appPageScreenSub'])}"
            f"&appFlow={self._app_flow}"
        )

    def _check_session_alive(self, html: str, context: str) -> None:
        if looks_like_login_page(html):
            raise SessionExpiredError(f"{context}: got the login page back")

    async def fetch_history_page(self) -> str:
        """The daily Usage History page (default window, all services shown)."""
        body, _, status = await self._get(self._history_base())
        if status >= 400:
            raise TransportError("history page fetch failed", status=status)
        self._check_session_alive(body, "history page")
        return body

    async def _fetch_transition(self, transition: str, service: str) -> str:
        url = (
            f"{self._history_base()}"
            f"&appTransition={quote_plus(transition)}&Service={quote_plus(service)}"
        )
        body, _, status = await self._get(url)
        if status >= 400:
            raise TransportError(f"{transition} fetch failed", status=status)
        self._check_session_alive(body, transition)
        return body

    async def fetch_interval_page(self, service: str = "Electric") -> str:
        """The 15-minute grid (7-day rolling window; the archive's main food)."""
        return await self._fetch_transition(TRANSITION_15MIN, service)

    async def fetch_hourly_page(self, service: str = "Electric") -> str:
        """The hourly grid (strict subset of 15-min; fetched only for probes)."""
        return await self._fetch_transition(TRANSITION_HOURLY, service)

    async def post_daily_history(
        self,
        from_date: str,
        to_date: str,
        service_type: str = "Electric",
        history_html: str | None = None,
    ) -> str:
        """POST the date-range form (MM/DD/YYYY dates). Proven for Water;
        Electric support is exactly what probe P4 establishes."""
        page = history_html if history_html is not None else await self.fetch_history_page()
        form = find_form_with_fields(page, CSRF_FIELDS)
        payload = {
            "selectedTimePeriod": "4",
            "FromDate": from_date,
            "ToDate": to_date,
            "ServiceType": service_type,
            "action": "Load",
            "SubmitButtonValidateTimePeriod": SUBMIT_VALIDATE_TIME_PERIOD,
            "cf_CSRFToken": form.inputs["cf_CSRFToken"],
            "cf_CSRFToken_web": form.inputs["cf_CSRFToken_web"],
        }
        headers = dict(HTML_HEADERS)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Referer"] = self._history_base()
        body, _, status = await self._post(self._history_base(), payload, headers)
        if status >= 400:
            raise TransportError("history POST failed", status=status)
        self._check_session_alive(body, "history POST")
        return body

    async def fetch_app_url(self, url: str) -> str:
        """Fetch an in-app URL discovered on a page (settings probe etc.)."""
        body, _, status = await self._get(url)
        if status >= 400:
            raise TransportError("app page fetch failed", status=status, url=scrub_url(url))
        return body
