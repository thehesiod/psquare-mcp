"""Tests for the PS_NO_ELICIT unattended-MFA escape hatch.

_handle_mfa normally prompts for the code inline via ctx.elicit(). That call
blocks until the client answers, which never happens for an unattended caller
(a claude.ai routine, a cron job), so the tool call hangs until the elicitation
times out. PS_NO_ELICIT makes it return the MFARequiredError text immediately
so the caller can fetch the code out of band and call submit_mfa_code.

The property under test is not just the return value but that ctx.elicit() is
never awaited — returning the right string after blocking for 60s would still
be the bug.
"""

import asyncio
from types import SimpleNamespace

import pytest

from parentsquare_mcp.auth import MFARequiredError, MFAState
from parentsquare_mcp.server import _handle_mfa


class ExplodingContext:
    """A Context whose elicit() fails the test if it is ever awaited."""

    def __init__(self):
        self.elicit_calls = 0

    async def elicit(self, message, schema):
        self.elicit_calls += 1
        raise AssertionError("ctx.elicit() must not be called when PS_NO_ELICIT is set")


class AcceptingContext:
    """A Context whose elicit() accepts with a fixed code."""

    def __init__(self, code="123456"):
        self.code = code
        self.elicit_calls = 0

    async def elicit(self, message, schema):
        self.elicit_calls += 1
        return SimpleNamespace(action="accept", data=SimpleNamespace(code=self.code))


def _app():
    return SimpleNamespace(mfa_state=None, client=SimpleNamespace(mfa_state=None))


def _exc():
    return MFARequiredError(MFAState(
        contact_value="j***@example.com",
        contact_method="email",
        email="jason@example.com",
        csrf_token="tok",
    ))


def test_no_elicit_returns_message_without_eliciting(monkeypatch):
    monkeypatch.setenv("PS_NO_ELICIT", "1")
    app, ctx, exc = _app(), ExplodingContext(), _exc()

    result = asyncio.run(_handle_mfa(app, exc, ctx))

    assert ctx.elicit_calls == 0
    assert result == str(exc)
    assert "submit_mfa_code" in result


def test_no_elicit_still_stores_mfa_state(monkeypatch):
    """The caller needs the state persisted so a later submit_mfa_code works."""
    monkeypatch.setenv("PS_NO_ELICIT", "1")
    app, exc = _app(), _exc()

    asyncio.run(_handle_mfa(app, exc, ExplodingContext()))

    assert app.mfa_state is exc.mfa_state
    assert app.client.mfa_state is exc.mfa_state


@pytest.mark.parametrize("value", ["1", "true", "0", "false", "no"])
def test_any_non_empty_value_disables_elicitation(monkeypatch, value):
    """The check is presence-based, so even "0"/"false" disable elicitation.

    Documented deliberately: PS_NO_ELICIT=0 does *not* re-enable prompting.
    Callers must unset the variable.
    """
    monkeypatch.setenv("PS_NO_ELICIT", value)
    ctx = ExplodingContext()

    result = asyncio.run(_handle_mfa(_app(), _exc(), ctx))

    assert ctx.elicit_calls == 0
    assert "MFA verification required" in result


def test_empty_value_leaves_elicitation_enabled(monkeypatch):
    monkeypatch.setenv("PS_NO_ELICIT", "")
    app, ctx = _app(), AcceptingContext()
    submitted = []
    monkeypatch.setattr(
        "parentsquare_mcp.server.submit_mfa",
        lambda session, state, code: submitted.append(code),
    )
    app.client = SimpleNamespace(
        mfa_state=None,
        session=object(),
        invalidate_csrf_token=lambda: None,
    )

    result = asyncio.run(_handle_mfa(app, _exc(), ctx))

    assert ctx.elicit_calls == 1
    assert submitted == ["123456"]
    assert result == ""  # empty string signals success -> caller retries


def test_unset_leaves_elicitation_enabled(monkeypatch):
    monkeypatch.delenv("PS_NO_ELICIT", raising=False)
    app, ctx = _app(), AcceptingContext()
    monkeypatch.setattr("parentsquare_mcp.server.submit_mfa", lambda *a, **k: None)
    app.client = SimpleNamespace(
        mfa_state=None,
        session=object(),
        invalidate_csrf_token=lambda: None,
    )

    result = asyncio.run(_handle_mfa(app, _exc(), ctx))

    assert ctx.elicit_calls == 1
    assert result == ""
