"""Tests for parentsquare-export-cookies console output encoding.

The helper prints instructions and then blocks on input(). On a Windows console
using the legacy cp1252 code page, a print() containing a non-ASCII character
raises UnicodeEncodeError -- and it did so *before* the input() prompt, so the
user could never paste a cookie and no file was written. A decorative arrow
therefore made the whole script unusable on Windows.

These tests run the full flow and assert the captured output survives a cp1252
encode, so reintroducing a stray "->"-style Unicode character fails here rather
than in a bug report.
"""

import json

import pytest

from parentsquare_mcp import export_cookies

COOKIE_HEADER = "ps_s=abc123; _ga=GA1.2.3; other=value"


@pytest.fixture
def cookie_file(tmp_path, monkeypatch):
    """Redirect COOKIE_FILE so tests never touch the real ~/.parentsquare_cookies.json."""
    path = tmp_path / "cookies.json"
    monkeypatch.setattr(export_cookies, "COOKIE_FILE", path)
    return path


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    monkeypatch.setattr(export_cookies.webbrowser, "open", lambda url: True)


def _run(monkeypatch, raw):
    monkeypatch.setattr("builtins.input", lambda prompt="": raw)
    export_cookies.export_from_devtools()


def test_full_run_output_is_cp1252_safe(monkeypatch, capsys, cookie_file):
    """The regression test for the Windows crash."""
    _run(monkeypatch, COOKIE_HEADER)
    out = capsys.readouterr().out

    out.encode("cp1252")  # raises UnicodeEncodeError on regression
    assert "-> Copy value" in out


def test_missing_session_cookie_warning_is_cp1252_safe(monkeypatch, capsys, cookie_file):
    """The warning path carried an emoji, so it needs its own coverage."""
    _run(monkeypatch, "_ga=GA1.2.3")
    out = capsys.readouterr().out

    out.encode("cp1252")
    assert "Warning: ps_s (session) cookie not found." in out


@pytest.mark.parametrize(
    "name",
    ["INSTRUCTIONS", "INSTRUCTIONS_AFTER_BROWSER", "WARN_NO_SESSION_COOKIE"],
)
def test_message_blocks_are_ascii(name):
    for line in getattr(export_cookies, name):
        line.encode("ascii")


@pytest.mark.parametrize("name", ["ERR_NO_INPUT", "ERR_NO_COOKIES"])
def test_error_messages_are_ascii(name):
    getattr(export_cookies, name).encode("ascii")


def test_cookies_are_written(monkeypatch, capsys, cookie_file):
    _run(monkeypatch, COOKIE_HEADER)

    saved = json.loads(cookie_file.read_text())
    assert set(saved) == {"ps_s", "_ga", "other"}
    assert saved["ps_s"]["value"] == "abc123"
    assert saved["ps_s"]["domain"] == ".parentsquare.com"
    assert saved["ps_s"]["secure"] is True


def test_empty_input_aborts(monkeypatch, capsys, cookie_file):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "   ")
    assert exc.value.code == 1
    assert not cookie_file.exists()
    capsys.readouterr().out.encode("cp1252")


def test_unparseable_input_aborts(monkeypatch, capsys, cookie_file):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "no-equals-sign-here")
    assert exc.value.code == 1
    assert not cookie_file.exists()
    capsys.readouterr().out.encode("cp1252")


def test_force_utf8_stdout_tolerates_non_reconfigurable_streams():
    """pytest replaces stdout with a non-TextIOWrapper; this must not explode."""
    export_cookies._force_utf8_stdout()
