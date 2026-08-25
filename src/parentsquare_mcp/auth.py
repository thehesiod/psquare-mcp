from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from parentsquare_mcp.config import BASE_URL

logger = logging.getLogger(__name__)

COOKIE_FILE = Path(os.environ.get("PS_COOKIE_FILE", "~/.parentsquare_cookies.json")).expanduser()
MFA_STATE_FILE = COOKIE_FILE.with_name(".parentsquare_mfa_state.json")


@dataclass
class MFAState:
    """Stores state needed to complete MFA verification."""

    contact_value: str  # masked email/phone from redirect
    contact_method: str  # "email" or "phone"
    email: str  # the actual email used to login
    csrf_token: str = ""  # CSRF token from the MFA page — required for /mfa/submit

    def save(self) -> None:
        """Persist MFA state to disk so it survives server restarts."""
        MFA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        MFA_STATE_FILE.write_text(json.dumps({
            "contact_value": self.contact_value,
            "contact_method": self.contact_method,
            "email": self.email,
            "csrf_token": self.csrf_token,
        }))
        logger.info(f"Saved MFA state to {MFA_STATE_FILE}")

    @classmethod
    def load(cls) -> MFAState | None:
        """Load persisted MFA state from disk. Returns None if not found."""
        if not MFA_STATE_FILE.exists():
            return None
        try:
            data = json.loads(MFA_STATE_FILE.read_text())
            return cls(
                contact_value=data["contact_value"],
                contact_method=data["contact_method"],
                email=data["email"],
                csrf_token=data.get("csrf_token", ""),
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load MFA state: {e}")
            return None

    @staticmethod
    def clear() -> None:
        """Remove persisted MFA state file."""
        if MFA_STATE_FILE.exists():
            MFA_STATE_FILE.unlink()
            logger.info("Cleared MFA state file")


class MFARequiredError(Exception):
    """Raised when login succeeds but MFA verification is needed."""

    def __init__(self, mfa_state: MFAState):
        self.mfa_state = mfa_state
        masked = mfa_state.contact_value
        method = mfa_state.contact_method
        super().__init__(
            f"MFA verification required. A 6-digit code was sent to your {method} ({masked}). "
            f"Use the submit_mfa_code tool to provide the code."
        )


def load_credentials_from_env() -> tuple[str, str] | None:
    """Load credentials from PS_USERNAME / PS_PASSWORD environment variables."""
    username = os.environ.get("PS_USERNAME")
    password = os.environ.get("PS_PASSWORD")
    if username and password:
        logger.info("Loaded credentials from environment variables")
        return username, password
    if username or password:
        logger.warning("PS_USERNAME/PS_PASSWORD partially set — both required; falling back to 1Password")
    return None


def load_credentials_from_lastpass() -> tuple[str, str]:
    """Load ParentSquare credentials from the LastPass CLI (``lpass``).

    Requires the user to have run ``lpass login <email>`` in a terminal
    beforehand. The item to read is controlled by ``PS_LASTPASS_ITEM``
    (default ``parentsquare.com``) and may be an exact entry name or an entry ID.

    Credential values are never included in log output or error messages.
    """
    item = os.environ.get("PS_LASTPASS_ITEM", "parentsquare.com")

    # Fast, non-interactive login-state check. A short timeout prevents an
    # MCP request from hanging if lpass would otherwise block on a prompt.
    try:
        status = subprocess.run(
            ["lpass", "status", "--quiet"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "LastPass CLI (lpass) not found. Install it (e.g. `brew install lastpass-cli`) "
            "or set PS_CREDENTIAL_PROVIDER to another provider."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Timed out running `lpass status`. Is the LastPass agent responsive?") from e

    if status.returncode != 0:
        raise RuntimeError(
            "Not logged in to LastPass. Run `lpass login <your-lastpass-email>` in a terminal "
            "before starting the server (this may prompt for MFA)."
        )

    try:
        result = subprocess.run(
            ["lpass", "show", "--json", "--sync=auto", item],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Timed out reading '{item}' from LastPass.") from e

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to read '{item}' from LastPass. Check that the item exists and PS_LASTPASS_ITEM is correct."
        )

    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse LastPass response for '{item}'.") from e

    if not isinstance(entries, list) or len(entries) == 0:
        raise RuntimeError(f"No LastPass entry named '{item}' was found.")
    if len(entries) > 1:
        raise RuntimeError(
            f"Multiple LastPass entries match '{item}'. Set PS_LASTPASS_ITEM to a unique name or entry ID."
        )

    entry = entries[0]
    username = entry.get("username") or ""
    password = entry.get("password") or ""
    if not username or not password:
        raise RuntimeError(f"LastPass entry '{item}' is missing a username or password field.")

    logger.info("Loaded credentials from LastPass")
    return username, password


def load_credentials_from_1password() -> tuple[str, str]:
    """Load ParentSquare credentials from 1Password via CLI."""
    result = subprocess.run(
        ["op", "item", "get", "Parentsquare", "--fields", "label=username,label=password", "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    fields = json.loads(result.stdout)
    creds: dict[str, str] = {}
    for field_obj in fields:
        label = field_obj.get("label", "")
        value = field_obj.get("value", "")
        if label in ("username", "password"):
            creds[label] = value
    if "username" not in creds or "password" not in creds:
        raise RuntimeError(f"Could not find username/password in 1Password. Got fields: {list(creds.keys())}")
    return creds["username"], creds["password"]


def load_credentials() -> tuple[str, str]:
    """Load credentials from env vars, then the configured credential provider.

    Order:
      1. A complete PS_USERNAME / PS_PASSWORD pair (highest priority).
      2. The provider named by PS_CREDENTIAL_PROVIDER: ``lastpass`` or
         ``1password`` (default ``1password`` for backward compatibility).
    """
    if creds := load_credentials_from_env():
        return creds

    provider = os.environ.get("PS_CREDENTIAL_PROVIDER", "1password").strip().lower()
    if provider == "lastpass":
        return load_credentials_from_lastpass()
    if provider == "1password":
        return load_credentials_from_1password()
    raise RuntimeError(
        f"Unknown PS_CREDENTIAL_PROVIDER '{provider}'. Expected 'lastpass' or '1password'."
    )


def save_cookies(session: requests.Session) -> None:
    """Persist session cookies to disk for reuse across server restarts."""
    cookies = {}
    for cookie in session.cookies:
        cookies[cookie.name] = {
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
        }
    COOKIE_FILE.write_text(json.dumps(cookies, indent=2))
    logger.info(f"Saved {len(cookies)} cookies to {COOKIE_FILE}")


def load_cookies(session: requests.Session) -> bool:
    """Load previously saved cookies. Returns True if cookies were loaded."""
    if not COOKIE_FILE.exists():
        return False
    try:
        cookies = json.loads(COOKIE_FILE.read_text())
        for name, data in cookies.items():
            session.cookies.set(
                name,
                data["value"],
                domain=data.get("domain", ".parentsquare.com"),
                path=data.get("path", "/"),
            )
        logger.info(f"Loaded {len(cookies)} cookies from {COOKIE_FILE}")
        return True
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Failed to load cookies: {e}")
        return False


def extract_csrf_token(session: requests.Session) -> str:
    """GET /signin and extract CSRF token from <meta name='csrf-token'> tag."""
    resp = session.get(f"{BASE_URL}/signin")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if not meta or not meta.get("content"):
        raise RuntimeError("Could not find CSRF token on signin page")
    return meta["content"]


def login(session: requests.Session, email: str, password: str) -> None:
    """Perform full login flow: extract CSRF, POST credentials, verify success.

    If 2FA is required, raises MFARequiredError with state needed to complete
    verification via submit_mfa_code().
    """
    logger.info("Logging in to ParentSquare...")
    csrf = extract_csrf_token(session)
    resp = session.post(
        f"{BASE_URL}/sessions",
        data={
            "utf8": "✓",
            "authenticity_token": csrf,
            "session[email]": email,
            "session[password]": password,
            "commit": "Sign In",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{BASE_URL}/signin",
        },
        allow_redirects=True,
    )

    # After successful login, should redirect away from /signin
    if "mfa_required" in resp.url:
        # Parse MFA redirect params: /signin?mfa_required=true&contact_value=...&contact_method=email
        parsed = urlparse(resp.url)
        params = parse_qs(parsed.query)
        contact_value = params.get("contact_value", [""])[0]
        contact_method = params.get("contact_method", ["email"])[0]
        logger.info(f"MFA required — code sent to {contact_method}: {contact_value}")

        # Extract CSRF token from the MFA page — Rails requires this for /mfa/submit
        mfa_soup = BeautifulSoup(resp.text, "html.parser")
        csrf_meta = mfa_soup.find("meta", attrs={"name": "csrf-token"})
        csrf_token = csrf_meta["content"] if csrf_meta and csrf_meta.get("content") else ""
        if csrf_token:
            logger.info("Captured CSRF token from MFA page")
        else:
            logger.warning("No CSRF token found on MFA page")

        # Save cookies from login attempt — needed for /mfa/submit
        save_cookies(session)

        mfa_state = MFAState(
            contact_value=contact_value,
            contact_method=contact_method,
            email=email,
            csrf_token=csrf_token,
        )
        # Persist MFA state so it survives server restarts
        mfa_state.save()
        raise MFARequiredError(mfa_state)

    if "/signin" in resp.url:
        raise RuntimeError("Login failed — redirected back to signin. Check credentials.")

    logger.info("Successfully logged in to ParentSquare")
    save_cookies(session)


def submit_mfa(session: requests.Session, mfa_state: MFAState, code: str) -> None:
    """Submit a 6-digit MFA verification code to complete login.

    Args:
        session: The requests session (must already have cookies from login attempt)
        mfa_state: MFA state from the login redirect
        code: The 6-digit verification code from email/phone
    """
    logger.info("Submitting MFA verification code...")
    payload: dict[str, str] = {
        "data_value": mfa_state.contact_value,
        "code": code,
    }
    if mfa_state.contact_method == "email":
        payload["email"] = mfa_state.email
    else:
        payload["phone"] = mfa_state.contact_value

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/signin",
    }
    if mfa_state.csrf_token:
        headers["X-CSRF-Token"] = mfa_state.csrf_token

    resp = session.post(
        f"{BASE_URL}/mfa/submit",
        json=payload,
        headers=headers,
    )

    if resp.status_code == 401:
        raise RuntimeError(
            "MFA verification failed — invalid or expired code. "
            "Check your email for the latest code and try submit_mfa_code again."
        )
    resp.raise_for_status()

    data = resp.json()
    redirect_url = data.get("redirect_url", "")
    if redirect_url:
        # Follow the redirect to establish the full session
        if redirect_url.startswith("/"):
            redirect_url = f"{BASE_URL}{redirect_url}"
        session.get(redirect_url)

    # Verify the MFA actually established an authenticated session
    if not is_session_valid(session):
        raise RuntimeError(
            "MFA code was accepted but session is not authenticated. "
            "The code may have expired. Try logging in again to get a new code."
        )

    logger.info("MFA verification successful")
    save_cookies(session)
    MFAState.clear()


def is_session_valid(session: requests.Session) -> bool:
    """Check whether the session is authenticated.

    ParentSquare returns 200 on the root page even without auth, so we can't
    rely on a /signin redirect. Instead, check for a numeric gon.user_id in the
    page. Unauthenticated pages render ``gon.user_id=null``, so a mere substring
    check for ``gon.user_id`` is not sufficient — the value must be an integer.

    **Do not add a ``<title>``-based check here.** It is tempting (and has been
    suggested) to treat a page titled "Sign In | ..." as unauthenticated, but
    verified live: the root page of a genuinely authenticated session is titled
    ``Sign In | <School> | ParentSquare`` while carrying ``gon.user_id=<int>``,
    no password form, and full access to ``/schools/{id}/feeds``. Keying on the
    title would misclassify a working session and force endless re-logins.
    Pinned by tests/test_user_agent.py::test_signin_title_with_user_id_is_valid.
    """
    resp = session.get(f"{BASE_URL}/", allow_redirects=True)
    if "/signin" in resp.url:
        return False
    if resp.status_code != 200:
        return False
    return re.search(r"gon\.user_id\s*=\s*\d+", resp.text) is not None


def ensure_session(session: requests.Session, email: str, password: str) -> None:
    """Re-login if session has expired."""
    if not is_session_valid(session):
        logger.info("Session expired, re-authenticating...")
        login(session, email, password)
