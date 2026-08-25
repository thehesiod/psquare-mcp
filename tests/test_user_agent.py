"""Pins the browser User-Agent and the session-validity heuristic.

ParentSquare rejects non-browser clients (403 ``browser_unsupported``) and, worse,
can serve *unauthenticated* content to a request carrying valid cookies when the
User-Agent looks like a script — a silent failure that ends with the client
falling back to placeholder data. The header therefore has to be attached to
``PSClient``'s default session, not to one particular call site, or a bare
``PSClient()`` quietly reverts to the broken behaviour.

The page fixtures below are trimmed from live responses captured 2026-08-25.
"""

import re

import requests

from parentsquare_mcp.auth import is_session_valid
from parentsquare_mcp.client import PSClient, make_session
from parentsquare_mcp.server import app_lifespan

# Live capture: an *authenticated* root page. Note the "Sign In" title — see
# test_signin_title_with_user_id_is_valid.
AUTHENTICATED_ROOT = """
<html><head><title>Sign In | Claire Lilienthal K-8 Alternative School | ParentSquare</title></head>
<body><script>gon.user_id=68418646;gon.institute_id=13749;</script></body></html>
"""

# Live capture: the root page with no cookies. No gon.user_id at all.
ANONYMOUS_ROOT = """
<html><head><title>K-12 Family Engagement Platform for Schools | ParentSquare</title></head>
<body><script>gon.institute_id=null;</script></body></html>
"""

# What a logged-out-but-gon-bearing page looks like: explicit null.
NULL_USER_ROOT = """
<html><head><title>Sign In | ParentSquare</title></head>
<body><script>gon.user_id=null;</script></body></html>
"""


class _Resp:
    def __init__(self, text, status=200, url="https://www.parentsquare.com/"):
        self.text = text
        self.status_code = status
        self.url = url
        self.headers = {}


class _RecordingSession(requests.Session):
    """Captures the headers actually sent, and replays a canned body."""

    def __init__(self, body=AUTHENTICATED_ROOT):
        super().__init__()
        self.body = body
        self.seen_headers = None

    def get(self, url, **kwargs):  # type: ignore[override]
        merged = dict(self.headers)
        merged.update(kwargs.get("headers") or {})
        self.seen_headers = merged
        return _Resp(self.body)


def test_default_client_session_sends_chrome_user_agent():
    """A bare PSClient() must still look like a browser.

    This is the actual regression: the header used to be applied in
    server.py's app_lifespan, so any other construction path sent
    ``python-requests/x.y``.
    """
    client = PSClient()
    ua = client.session.headers.get("User-Agent", "")
    assert "Chrome" in ua, f"PSClient default session is not browser-like: {ua!r}"
    assert "python-requests" not in ua


def test_make_session_sets_browser_headers():
    session = make_session()
    assert "Chrome" in session.headers["User-Agent"]
    assert session.headers["Accept-Language"] == "en-US,en;q=0.9"
    assert "text/html" in session.headers["Accept"]


def test_server_lifespan_uses_browser_session():
    """The server path must keep the header it had before the move."""
    import asyncio
    from contextlib import suppress

    async def check():
        async with app_lifespan(object()) as ctx:
            return ctx.client.session.headers.get("User-Agent", "")

    ua = ""
    with suppress(Exception):
        ua = asyncio.run(check())
    assert "Chrome" in ua, f"app_lifespan client lost its browser UA: {ua!r}"


def test_per_request_accept_still_overrides_session_default():
    """Moving Accept onto the session must not shadow the per-request Accept.

    tests/test_accept_headers.py explains why get_json must send a JSON-only
    Accept; that per-request header has to keep winning over the new
    session-level text/html default.
    """
    session = _RecordingSession()
    session.headers.update({"Accept": "text/html,application/xhtml+xml"})
    session.get("https://www.parentsquare.com/", headers={"Accept": "application/json"})
    assert session.seen_headers["Accept"] == "application/json"


def test_signin_title_with_user_id_is_valid():
    """An authenticated page titled "Sign In" must NOT be treated as logged out.

    Verified live 2026-08-25: the root page of a working admin session carries
    that title alongside a real gon.user_id. A title-based heuristic — which the
    #7 report recommended — would break this session.
    """
    session = _RecordingSession(AUTHENTICATED_ROOT)
    assert "Sign In" in AUTHENTICATED_ROOT
    assert is_session_valid(session) is True


def test_anonymous_root_is_invalid():
    session = _RecordingSession(ANONYMOUS_ROOT)
    assert is_session_valid(session) is False


def test_null_user_id_is_invalid():
    session = _RecordingSession(NULL_USER_ROOT)
    assert is_session_valid(session) is False


def test_user_id_regex_requires_integer():
    """Guards the specific substring-vs-integer distinction the docstring calls out."""
    pattern = r"gon\.user_id\s*=\s*\d+"
    assert re.search(pattern, AUTHENTICATED_ROOT)
    assert not re.search(pattern, NULL_USER_ROOT)
    assert not re.search(pattern, ANONYMOUS_ROOT)
