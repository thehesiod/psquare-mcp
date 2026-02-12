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


def export_from_devtools() -> None:
    """Guide user to copy Cookie header from Chrome DevTools Network tab."""
    print("=" * 60)
    print("ParentSquare Cookie Export")
    print("=" * 60)
    print()
    print("Step 1: Opening ParentSquare in your browser...")
    webbrowser.open(f"{BASE_URL}/signin")
    print()
    print("Step 2: Log in and complete 2FA verification.")
    print()
    print("Step 3: After login, open Chrome DevTools (F12 or Cmd+Opt+I)")
    print("        Go to the Network tab.")
    print()
    print("Step 4: Click on any request to www.parentsquare.com")
    print("        (reload the page if the Network tab is empty)")
    print()
    print("Step 5: In the request Headers section, find the 'cookie:' header")
    print("        Right-click the value → Copy value")
    print()
    print("Step 6: Paste the cookie string below and press Enter:")
    print()

    raw = input("> ").strip()
    if not raw:
        print("\nNo input received. Aborting.")
        sys.exit(1)

    # Parse "name=value; name2=value2; ..." format
    cookies = {}
    for pair in raw.split("; "):
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        # Keep values as-is (URL-encoded) — Rails cookies contain special chars
        cookies[name] = {
            "value": value,
            "domain": ".parentsquare.com",
            "path": "/",
            "secure": True,
        }

    if not cookies:
        print("\nNo cookies parsed. Make sure you copied the cookie header value.")
        sys.exit(1)

    # Check for the critical session cookie
    if "ps_s" not in cookies:
        print("\n⚠️  Warning: ps_s (session) cookie not found.")
        print("   Make sure you copied from the Network tab, not the Console.")

    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(json.dumps(cookies, indent=2))
    print(f"\n✅ Saved {len(cookies)} cookies to {COOKIE_FILE}")
    print("   You can now start the MCP server.")


def main() -> None:
    export_from_devtools()


if __name__ == "__main__":
    main()
