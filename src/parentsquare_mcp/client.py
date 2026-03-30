from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from parentsquare_mcp.auth import MFAState, save_cookies
from parentsquare_mcp.config import BASE_URL

logger = logging.getLogger(__name__)


@dataclass
class AccountInfo:
    """Discovered account info: user, schools, and students."""

    user_id: int = 0
    schools: dict[int, str] = field(default_factory=dict)  # {school_id: name}
    students: dict[int, dict] = field(default_factory=dict)  # {student_id: {name, school_id, grade}}


@dataclass
class PSClient:
    """HTTP client wrapper for ParentSquare with auto re-login on session expiry."""

    session: requests.Session = field(default_factory=requests.Session)
    mfa_state: MFAState | None = None
    account: AccountInfo = field(default_factory=AccountInfo)

    def _relogin(self) -> None:
        """Load credentials from 1Password and re-authenticate.

        Raises MFARequiredError if 2FA is needed — caller should store the
        mfa_state and prompt the user for the code via submit_mfa_code tool.
        """
        from parentsquare_mcp.auth import load_credentials_from_1password, login

        logger.info("Session expired, loading credentials from 1Password...")
        email, password = load_credentials_from_1password()
        login(self.session, email, password)

    def _save_cookies_if_changed(self) -> None:
        """Persist cookies after each successful request to handle ps_s rotation."""
        try:
            save_cookies(self.session)
        except Exception:
            logger.debug("Failed to save cookies (non-fatal)", exc_info=True)

    def get_page(self, path: str, params: dict | None = None) -> BeautifulSoup:
        """GET a page and return parsed BeautifulSoup.

        Automatically re-authenticates if redirected to /signin.
        """
        url = f"{BASE_URL}{path}"
        resp = self.session.get(url, params=params)

        # Detect session expiry: redirect to signin
        if "/signin" in resp.url:
            self._relogin()
            resp = self.session.get(url, params=params)

        resp.raise_for_status()
        self._save_cookies_if_changed()
        return BeautifulSoup(resp.text, "html.parser")

    def _get_csrf_token(self) -> str:
        """Fetch a CSRF token from /dashboard. Re-authenticates if needed."""
        page_resp = self.session.get(f"{BASE_URL}/")
        if "/signin" in page_resp.url:
            self._relogin()
            page_resp = self.session.get(f"{BASE_URL}/")

        soup = BeautifulSoup(page_resp.text, "html.parser")
        csrf_meta = soup.find("meta", attrs={"name": "csrf-token"})
        return csrf_meta["content"] if csrf_meta else ""

    def graphql(self, query: str, variables: dict, operation_name: str) -> dict:
        """Execute a GraphQL query against /graphql."""
        csrf_token = self._get_csrf_token()

        resp = self.session.post(
            f"{BASE_URL}/graphql",
            json={"query": query, "variables": variables, "operationName": operation_name},
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf_token,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        resp.raise_for_status()
        self._save_cookies_if_changed()
        data = resp.json()
        if "errors" in data and data["errors"]:
            msg = data["errors"][0].get("message") or "GraphQL error"
            raise RuntimeError(f"GraphQL error: {msg}")
        return data.get("data", {})

    def post_json(self, path: str, payload: dict) -> dict:
        """POST JSON to an API endpoint. Fetches CSRF token automatically."""
        csrf_token = self._get_csrf_token()
        url = f"{BASE_URL}{path}"
        resp = self.session.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-CSRF-Token": csrf_token,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        resp.raise_for_status()
        self._save_cookies_if_changed()
        return resp.json()

    def get_json(self, path: str) -> dict:
        """GET a JSON API endpoint and return parsed response.

        Automatically re-authenticates if redirected to /signin.
        """
        url = f"{BASE_URL}{path}"
        resp = self.session.get(url, headers={"Accept": "application/json"})

        if "/signin" in resp.url:
            self._relogin()
            resp = self.session.get(url, headers={"Accept": "application/json"})

        resp.raise_for_status()
        self._save_cookies_if_changed()
        return resp.json()

    def get_raw(self, url: str, stream: bool = False) -> requests.Response:
        """GET a raw URL (for S3/CloudFront downloads). No base URL prepended."""
        resp = self.session.get(url, stream=stream)
        resp.raise_for_status()
        return resp

    def get_ics(self, path: str) -> str:
        """GET an ICS calendar endpoint and return raw text."""
        url = f"{BASE_URL}{path}"
        resp = self.session.get(url, headers={"Accept": "text/calendar"})

        if "/signin" in resp.url:
            self._relogin()
            resp = self.session.get(url, headers={"Accept": "text/calendar"})

        resp.raise_for_status()
        self._save_cookies_if_changed()
        return resp.text

    def discover_account(self) -> AccountInfo:
        """Auto-discover user ID, schools, and students from ParentSquare pages.

        Fetches the root page for gon.user_id/institute_id, then the school's
        feeds page for sidebar data (school name, student links, school switcher).
        """
        if self.account.user_id:
            return self.account

        # Root page has gon.user_id and gon.institute_id in script tags
        soup = self.get_page("/")

        # Extract user_id and current institute_id from gon.*
        current_school_id = 0
        for script in soup.find_all("script"):
            text = script.string or ""
            if "gon.user_id" in text:
                m = re.search(r"gon\.user_id=(\d+)", text)
                if m:
                    self.account.user_id = int(m.group(1))
                m = re.search(r"gon\.institute_id=(\d+)", text)
                if m:
                    current_school_id = int(m.group(1))
                break

        # If no user_id found, session is invalid — trigger re-login and retry
        if not self.account.user_id:
            logger.info("No gon.user_id found on root page — session not authenticated, re-logging in...")
            self._relogin()
            soup = self.get_page("/")
            for script in soup.find_all("script"):
                text = script.string or ""
                if "gon.user_id" in text:
                    m = re.search(r"gon\.user_id=(\d+)", text)
                    if m:
                        self.account.user_id = int(m.group(1))
                    m = re.search(r"gon\.institute_id=(\d+)", text)
                    if m:
                        current_school_id = int(m.group(1))
                    break

        if not current_school_id:
            logger.warning("Could not discover current school")
            return self.account

        # Get current school name from the API
        try:
            school_data = self.get_json(f"/api/v2/schools/{current_school_id}")
            current_name = school_data["data"]["attributes"]["name"]
        except Exception:
            current_name = f"School {current_school_id}"
        self.account.schools[current_school_id] = current_name

        # Feeds page has the sidebar with student links and school switcher
        feeds_soup = self.get_page(f"/schools/{current_school_id}/feeds")

        # Discover other schools via the school switcher AJAX endpoint
        switcher = feeds_soup.find("a", class_="toggle-children")
        if switcher:
            template_url = switcher.get("data-remote-template", "")
            if template_url:
                try:
                    switch_soup = self.get_page(template_url.split(".com")[-1] if ".com" in template_url else template_url)
                    for a in switch_soup.find_all("a", href=re.compile(r"/schools/(\d+)")):
                        m = re.search(r"/schools/(\d+)", a["href"])
                        if m:
                            sid = int(m.group(1))
                            if sid not in self.account.schools:
                                # Get name from API
                                try:
                                    sd = self.get_json(f"/api/v2/schools/{sid}")
                                    self.account.schools[sid] = sd["data"]["attributes"]["name"]
                                except Exception:
                                    self.account.schools[sid] = a.get_text(strip=True)
                except Exception:
                    logger.debug("Failed to load school switcher", exc_info=True)

        # Discover students from sidebar links
        for a in feeds_soup.find_all("a", href=re.compile(r"/students/(\d+)/dashboard")):
            m = re.search(r"/students/(\d+)", a["href"])
            if not m:
                continue
            student_id = int(m.group(1))
            name_el = a.find("h4")
            name = name_el.get_text(strip=True) if name_el else ""
            detail_el = a.find("div", class_="truncate-text")
            detail = detail_el.get_text(strip=True) if detail_el else ""
            # Parse "1st Grade • School Name"
            grade, school_name = "", ""
            if "•" in detail:
                parts = detail.split("•", 1)
                grade = parts[0].strip()
                school_name = parts[1].strip()
            # Find school_id by matching name (case-insensitive substring)
            school_id = 0
            school_lower = school_name.lower()
            for sid, sname in self.account.schools.items():
                if sname.lower() == school_lower or school_lower in sname.lower():
                    school_id = sid
                    break
            self.account.students[student_id] = {
                "name": name,
                "school_id": school_id,
                "grade": grade,
            }

        logger.info(
            f"Discovered account: user_id={self.account.user_id}, "
            f"{len(self.account.schools)} schools, {len(self.account.students)} students"
        )
        return self.account
