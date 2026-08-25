from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from parentsquare_mcp.auth import MFAState, save_cookies
from parentsquare_mcp.config import BASE_URL, URLS

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
    _csrf_token: str | None = field(default=None, repr=False)

    def _relogin(self) -> None:
        """Load credentials from 1Password and re-authenticate.

        Raises MFARequiredError if 2FA is needed — caller should store the
        mfa_state and prompt the user for the code via submit_mfa_code tool.
        """
        from parentsquare_mcp.auth import load_credentials, login

        logger.info("Session expired, loading credentials...")
        self.invalidate_csrf_token()
        email, password = load_credentials()
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

    def invalidate_csrf_token(self) -> None:
        """Drop the cached CSRF token so the next write fetches a fresh one.

        Call this after any out-of-band re-authentication (MFA completion),
        since a new session invalidates the old token.
        """
        self._csrf_token = None

    def _get_csrf_token(self, force_refresh: bool = False) -> str:
        """Return a CSRF token, fetching one from the dashboard if needed.

        The token is cached for the life of the session: Rails derives it from a
        per-session secret, so it stays valid across requests even though the
        ``ps_s`` cookie rotates. Without the cache every single write costs an
        extra ``GET /``. Callers that see a token rejected should retry once with
        ``force_refresh=True`` — see ``_with_csrf``.
        """
        if self._csrf_token and not force_refresh:
            return self._csrf_token

        page_resp = self.session.get(f"{BASE_URL}/")
        if "/signin" in page_resp.url:
            self._relogin()
            page_resp = self.session.get(f"{BASE_URL}/")

        soup = BeautifulSoup(page_resp.text, "html.parser")
        csrf_meta = soup.find("meta", attrs={"name": "csrf-token"})
        self._csrf_token = csrf_meta["content"] if csrf_meta else ""
        return self._csrf_token

    @staticmethod
    def _is_csrf_rejection(resp: requests.Response) -> bool:
        """Was this response a rejected/expired CSRF token or dead session?

        Deliberately narrow. A blanket retry on 422 would re-send writes that the
        server merely found invalid, so 4xx bodies only count when they name the
        token; an unauthenticated status or a bounce to /signin always counts.
        A rejected request never took effect, so retrying it is safe.
        """
        if "/signin" in resp.url:
            return True
        if resp.status_code in (401, 419):
            return True
        if resp.status_code in (403, 422):
            body = (resp.text or "")[:2000].lower()
            return "authenticity" in body or "csrf" in body
        return False

    def _with_csrf(self, send) -> requests.Response:
        """Run ``send(csrf_token)``, retrying once with a fresh token if rejected.

        This is what makes caching safe: a stale token (or a session that died
        since the token was issued) costs one extra round trip instead of a
        failed write, and the refresh re-authenticates via ``_get_csrf_token``.
        """
        resp = send(self._get_csrf_token())
        if self._is_csrf_rejection(resp):
            logger.info("CSRF token rejected, refetching and retrying once")
            resp = send(self._get_csrf_token(force_refresh=True))
        return resp

    def graphql(self, query: str, variables: dict, operation_name: str) -> dict:
        """Execute a GraphQL query against /graphql."""
        resp = self._with_csrf(
            lambda csrf_token: self.session.post(
                f"{BASE_URL}/graphql",
                json={
                    "query": query,
                    "variables": variables,
                    "operationName": operation_name,
                },
                headers={
                    "Content-Type": "application/json",
                    "X-CSRF-Token": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
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
        url = f"{BASE_URL}{path}"
        resp = self._with_csrf(
            lambda csrf_token: self.session.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-CSRF-Token": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        )
        resp.raise_for_status()
        self._save_cookies_if_changed()
        return resp.json()

    def send_json(self, method: str, path: str, payload: dict) -> requests.Response:
        """Send a JSON body with an arbitrary verb and return the raw Response.

        Used by the JSON:API class/section admin endpoints, which take PATCH and
        PUT (e.g. ``PATCH /api/v2/sections/{id}``, ``PUT /api/v2/sections/{id}/staff``)
        and answer with real JSON rather than a Rails UJS script. Fetches the CSRF
        token automatically and does NOT raise on 4xx/5xx so callers can surface
        the API's own error payload (see ``parsers.classes.json_write_error``).
        """
        resp = self._with_csrf(
            lambda csrf_token: self.session.request(
                method.upper(),
                f"{BASE_URL}{path}",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-CSRF-Token": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": BASE_URL,
                },
            )
        )
        self._save_cookies_if_changed()
        return resp

    def post_json_raw(self, path: str, payload: dict) -> requests.Response:
        """POST a JSON body but return the raw Response (not parsed JSON).

        Some admin actions accept a JSON request yet reply with a Rails UJS
        ``text/javascript`` body (e.g. the bulk parent-invite endpoint), so
        ``post_json``'s ``resp.json()`` would raise. Fetches the CSRF token
        automatically and does NOT raise on 4xx/5xx — callers inspect the
        status/body (see ``write_succeeded``).
        """
        resp = self._with_csrf(
            lambda csrf_token: self.session.post(
                f"{BASE_URL}{path}",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-CSRF-Token": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": BASE_URL,
                },
            )
        )
        self._save_cookies_if_changed()
        return resp

    def post_form(self, path: str, data: dict) -> requests.Response:
        """POST an application/x-www-form-urlencoded body (Rails admin write form).

        Injects ``utf8=✓`` and ``authenticity_token`` and sends the CSRF token as
        a header — matching ParentSquare's admin roster forms. Returns the raw
        Response (these endpoints reply with a ``text/javascript`` UJS script, not
        JSON). Does NOT raise on 4xx/5xx so callers can inspect the body; use the
        status code and body to detect success.
        """
        resp = self._with_csrf(
            lambda csrf_token: self.session.post(
                f"{BASE_URL}{path}",
                data={"utf8": "\u2713", "authenticity_token": csrf_token, **data},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Accept": "*/*;q=0.5, text/javascript, application/javascript, "
                    "application/ecmascript, application/x-ecmascript",
                    "X-CSRF-Token": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": BASE_URL,
                },
            )
        )
        self._save_cookies_if_changed()
        return resp

    def get_text(self, path: str, params: dict | None = None) -> str:
        """GET a path and return the raw response text (e.g. an edit-form JS body).

        Automatically re-authenticates if redirected to /signin.
        """
        url = f"{BASE_URL}{path}"
        headers = {"Accept": "text/javascript", "X-Requested-With": "XMLHttpRequest"}
        resp = self.session.get(url, params=params, headers=headers)
        if "/signin" in resp.url:
            self._relogin()
            resp = self.session.get(url, params=params, headers=headers)
        resp.raise_for_status()
        self._save_cookies_if_changed()
        return resp.text

    def get_html(self, path: str, params: dict | None = None) -> str:
        """GET an HTML admin page and return the raw response text.

        Distinct from ``get_text``: this sends the session's normal browser
        ``Accept`` header rather than ``text/javascript`` + ``X-Requested-With``.
        Rails ``respond_to`` picks a format from those headers, so asking for JS
        on a plain HTML page yields a template-less 404 — the same trap
        documented on ``get_json``.
        """
        url = f"{BASE_URL}{path}"
        resp = self.session.get(url, params=params)
        if "/signin" in resp.url:
            self._relogin()
            resp = self.session.get(url, params=params)
        resp.raise_for_status()
        self._save_cookies_if_changed()
        return resp.text

    def get_json(self, path: str, params: dict | None = None) -> dict:
        """GET a JSON API endpoint and return parsed response.

        Automatically re-authenticates if redirected to /signin.
        """
        url = f"{BASE_URL}{path}"
        # Accept must stay JSON-only: the Rails roster feeds (e.g.
        # /schools/{id}/roster/parents_data) 404 if text/javascript is offered,
        # because respond_to then picks the JS format, which has no template.
        # Pinned by tests/test_accept_headers.py.
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        resp = self.session.get(url, params=params, headers=headers)

        if "/signin" in resp.url:
            self._relogin()
            resp = self.session.get(url, params=params, headers=headers)

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

    def _extract_gon(self, soup) -> tuple[int, int, str]:
        """Pull (user_id, institute_id, institute_type) out of the root page's gon.* vars.

        institute_type is "District" for district-level accounts and "School"
        (or absent) otherwise. It matters because gon.institute_id is a *district*
        id in the former case, and district ids 404 on every /schools/{id} route.
        """
        for script in soup.find_all("script"):
            text = script.string or ""
            if "gon.user_id" not in text:
                continue
            user_id = 0
            institute_id = 0
            m = re.search(r"gon\.user_id=(\d+)", text)
            if m:
                user_id = int(m.group(1))
            m = re.search(r"gon\.institute_id=(\d+)", text)
            if m:
                institute_id = int(m.group(1))
            m = re.search(r"""gon\.institute_type\s*=\s*["']?(\w+)""", text)
            institute_type = m.group(1) if m else ""
            return user_id, institute_id, institute_type
        return 0, 0, ""

    def _discover_district_schools(self, district_id: int) -> dict[int, str]:
        """Expand a district id into its member schools via the school-switcher fragment.

        A district-level parent's gon.institute_id is the district, which is not a
        school: /schools/{district_id}/feeds and /api/v2/schools/{district_id} both
        404, and the account's real schools are never found. The district switcher
        fragment lists them as /schools/{id}/feeds links.
        """
        schools: dict[int, str] = {}
        try:
            frag = self.get_page(URLS["district_schools"].format(institute_id=district_id))
        except Exception:
            logger.debug("Failed to load district school list", exc_info=True)
            return schools

        for a in frag.find_all("a", href=re.compile(r"/schools/\d+")):
            m = re.search(r"/schools/(\d+)", a["href"])
            if not m:
                continue
            sid = int(m.group(1))
            if sid not in schools:
                schools[sid] = a.get_text(strip=True)
        return schools

    def discover_account(self) -> AccountInfo:
        """Auto-discover user ID, schools, and students from ParentSquare pages.

        Fetches the root page for gon.user_id/institute_id, then the school's
        feeds page for sidebar data (school name, student links, school switcher).

        For district-level accounts gon.institute_id is a district rather than a
        school, so the member schools are resolved first and the first of them is
        used as the "current" school for the student-discovery pass.
        """
        if self.account.user_id:
            return self.account

        # Root page has gon.user_id and gon.institute_id in script tags
        soup = self.get_page("/")
        self.account.user_id, institute_id, institute_type = self._extract_gon(soup)

        # If no user_id found, session is invalid — trigger re-login and retry
        if not self.account.user_id:
            logger.info("No gon.user_id found on root page — session not authenticated, re-logging in...")
            self._relogin()
            soup = self.get_page("/")
            self.account.user_id, institute_id, institute_type = self._extract_gon(soup)

        current_school_id = institute_id
        if institute_type.lower() == "district" and institute_id:
            district_schools = self._discover_district_schools(institute_id)
            if district_schools:
                self.account.schools.update(district_schools)
                current_school_id = next(iter(district_schools))
                logger.info(
                    f"District account: expanded district {institute_id} "
                    f"into {len(district_schools)} schools"
                )
            else:
                # Nothing to fall back to: the district id itself is not a school.
                logger.warning(
                    f"District account (institute_id={institute_id}) but no member "
                    "schools found in the district switcher fragment"
                )
                return self.account

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
