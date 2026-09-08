"""Client login flow against a fake aiohttp session (no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from myusage_archive.client import (
    LoginMode,
    MyUsageClient,
    extract_app_flow,
    looks_like_login_page,
    looks_like_utility_select,
)
from myusage_archive.exceptions import (
    AuthenticationError,
    SessionExpiredError,
    UnsupportedAccountError,
)

LANDING_URL = (
    "https://www.myusage.com/data.cfm?appPage=Postpaid&appPageScreen="
    "&appPageScreenSub=&appFlow=2026090715343480&"
)
REDIRECT_URL = (
    "https://www.myusage.com/default.cfm?requestAction=Login"
    "&LoginEmail=WebSSOLogin&LoginPassword=00000000000000000000000000000000"
)


class FakeResponse:
    def __init__(self, body: str, url: str, status: int = 200) -> None:
        self._body = body
        self.url = url
        self.status = status

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeSession:
    """Routes requests by (method, exact-or-startswith URL)."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, FakeResponse]] = []
        self.requests: list[tuple[str, str, dict[str, str] | None]] = []

    def add(self, method: str, url_prefix: str, response: FakeResponse) -> None:
        self.routes.append((method, url_prefix, response))

    def _route(self, method: str, url: str) -> FakeResponse:
        for m, prefix, resp in self.routes:
            if m == method and url.startswith(prefix):
                return resp
        raise AssertionError(f"unrouted {method} {url}")

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append(("GET", url, kwargs.get("headers")))
        return self._route("GET", url)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append(("POST", url, kwargs.get("headers")))
        return self._route("POST", url)

    async def close(self) -> None:
        self.closed = True

    @property
    def cookie_jar(self) -> object:
        return object()


def make_session(
    login_json: dict[str, Any],
    landing_html: str = "<html><body>app</body></html>",
    landing_url: str = LANDING_URL,
) -> FakeSession:
    session = FakeSession()
    session.add("GET", "https://www.myusage.com/", FakeResponse("<html>home</html>", "https://www.myusage.com/"))
    session.add(
        "POST",
        "https://www.myusage.com/login",
        FakeResponse(json.dumps(login_json), "https://www.myusage.com/login"),
    )
    session.add("GET", REDIRECT_URL[:60], FakeResponse(landing_html, landing_url))
    # More specific homepage route must not shadow the redirect route: order matters,
    # so re-add homepage last for the priming GET only (prefix routing picks first match).
    session.routes.sort(key=lambda r: -len(r[1]))
    return session


def test_extract_app_flow() -> None:
    assert extract_app_flow(LANDING_URL) == "2026090715343480"
    assert extract_app_flow("https://x/data.cfm?foo=1") is None


def test_page_heuristics() -> None:
    assert looks_like_login_page('<input name="email"><input name="password">')
    assert not looks_like_login_page("<table id='grid15MinuteUsage'></table>")
    assert looks_like_utility_select("<h1>Select Utility</h1>")


async def test_canonical_login_success() -> None:
    session = make_session({"data": "ok", "redirect_url": REDIRECT_URL})
    client = MyUsageClient("u@example.com", "pw", session)  # type: ignore[arg-type]
    await client.login()
    assert client.app_flow == "2026090715343480"
    # Priming GET happened before the POST.
    methods = [m for m, _, _ in session.requests]
    assert methods[:2] == ["GET", "POST"]
    # XHR headers on the login POST.
    post_headers = session.requests[1][2]
    assert post_headers is not None
    assert post_headers.get("X-Requested-With") == "XMLHttpRequest"


async def test_login_failure_missing_redirect() -> None:
    session = make_session({"data": "Invalid email or password"})
    client = MyUsageClient("u@example.com", "pw", session)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError):
        await client.login()


async def test_login_failure_records_error_str_payload() -> None:
    """The observed live failure shape: 500 + {error_str, result}."""
    session = make_session({"error_str": "Too many attempts, wait", "result": False})
    client = MyUsageClient("u@example.com", "hunter2secret", session)  # type: ignore[arg-type]
    attempt = await client.attempt_login(LoginMode.CANONICAL)
    assert not attempt.ok
    assert attempt.error is not None
    assert "Too many attempts, wait" in attempt.error
    assert attempt.response_excerpt is not None
    assert "error_str" in attempt.response_excerpt
    assert "hunter2secret" not in attempt.response_excerpt


async def test_login_secret_never_in_error() -> None:
    session = make_session({"data": "bad creds for u@example.com hunter2secret"})
    client = MyUsageClient("u@example.com", "hunter2secret", session)  # type: ignore[arg-type]
    attempt = await client.attempt_login(LoginMode.CANONICAL)
    assert not attempt.ok
    assert attempt.error is not None
    assert "hunter2secret" not in attempt.error
    assert "u@example.com" not in attempt.error


async def test_utility_select_interstitial() -> None:
    session = make_session(
        {"redirect_url": REDIRECT_URL},
        landing_html="<html><h1>Select Utility</h1></html>",
        landing_url="https://www.myusage.com/default.cfm?x=1",
    )
    client = MyUsageClient("u@example.com", "pw", session)  # type: ignore[arg-type]
    with pytest.raises(UnsupportedAccountError):
        await client.login()


async def test_xhr_only_mode_skips_priming() -> None:
    session = make_session({"data": "ok", "redirect_url": REDIRECT_URL})
    client = MyUsageClient("u@example.com", "pw", session)  # type: ignore[arg-type]
    attempt = await client.attempt_login(LoginMode.XHR_ONLY)
    assert attempt.ok
    assert not attempt.primed
    assert session.requests[0][0] == "POST"


def test_history_base_requires_login() -> None:
    client = MyUsageClient("u@example.com", "pw", FakeSession())  # type: ignore[arg-type]
    with pytest.raises(SessionExpiredError):
        client._history_base()  # noqa: SLF001
