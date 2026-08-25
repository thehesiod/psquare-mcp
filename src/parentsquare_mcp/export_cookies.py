"""Helper script to export ParentSquare cookies from a browser session.

Usage:
    parentsquare-export-cookies

Opens ParentSquare in your default browser. After you complete login
(including 2FA), copy the Cookie header from DevTools Network tab and
paste it here. The httpOnly session cookie (ps_s) is not accessible
via document.cookie, so we need the full Cookie header.
"""

from __future__ import annotations

import json
import sys
import webbrowser

from parentsquare_mcp.auth import COOKIE_FILE
from parentsquare_mcp.config import BASE_URL

# Keep every byte printed by this script inside ASCII.
#
# This helper's whole job is to print instructions and then block on input().
# A Windows console using the legacy cp1252 code page raises UnicodeEncodeError
# on the first print() containing a non-ASCII character — which happened before
# the input() prompt was ever reached, so the user could not paste a cookie and
# no file was ever written. A decorative character therefore made the script
# unusable, not merely ugly. See test_export_cookies.py, which encodes these
# strings as cp1252 to keep them that way.
INSTRUCTIONS = [
    "=" * 60,
    "ParentSquare Cookie Export",
    "=" * 60,
    "",
    "Step 1: Opening ParentSquare in your browser...",
]

INSTRUCTIONS_AFTER_BROWSER = [
    "",
    "Step 2: Log in and complete 2FA verification.",
    "",
    "Step 3: After login, open Chrome DevTools (F12 or Cmd+Opt+I)",
    "        Go to the Network tab.",
    "",
    "Step 4: Click on any request to www.parentsquare.com",
    "        (reload the page if the Network tab is empty)",
    "",
    "Step 5: In the request Headers section, find the 'cookie:' header",
    "        Right-click the value -> Copy value",
    "",
    "Step 6: Paste the cookie string below and press Enter:",
    "",
]

WARN_NO_SESSION_COOKIE = [
    "",
    "Warning: ps_s (session) cookie not found.",
    "   Make sure you copied from the Network tab, not the Console.",
]

ERR_NO_INPUT = "\nNo input received. Aborting."
ERR_NO_COOKIES = "\nNo cookies parsed. Make sure you copied the cookie header value."


def _force_utf8_stdout() -> None:
    """Best-effort switch of stdout/stderr to UTF-8 with replacement.

    Belt and braces alongside the ASCII-only strings above: it keeps any future
    non-ASCII output (a school name, an error from ParentSquare) from killing
    the script on a legacy code page. Guarded with hasattr because stdout is not
    always a TextIOWrapper -- pytest's capture replaces it, for instance.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def export_from_devtools() -> None:
    """Guide user to copy Cookie header from Chrome DevTools Network tab."""
    for line in INSTRUCTIONS:
        print(line)
    webbrowser.open(f"{BASE_URL}/signin")
    for line in INSTRUCTIONS_AFTER_BROWSER:
        print(line)

    raw = input("> ").strip()
    if not raw:
        print(ERR_NO_INPUT)
        sys.exit(1)

    # Parse "name=value; name2=value2; ..." format
    cookies = {}
    for pair in raw.split("; "):
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        # Keep values as-is (URL-encoded) - Rails cookies contain special chars
        cookies[name] = {
            "value": value,
            "domain": ".parentsquare.com",
            "path": "/",
            "secure": True,
        }

    if not cookies:
        print(ERR_NO_COOKIES)
        sys.exit(1)

    # Check for the critical session cookie
    if "ps_s" not in cookies:
        for line in WARN_NO_SESSION_COOKIE:
            print(line)

    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(json.dumps(cookies, indent=2))
    print(f"\nSaved {len(cookies)} cookies to {COOKIE_FILE}")
    print("   You can now start the MCP server.")


def main() -> None:
    _force_utf8_stdout()
    export_from_devtools()


if __name__ == "__main__":
    main()
