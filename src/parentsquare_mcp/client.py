from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from parentsquare_mcp import auth
from parentsquare_mcp.auth import MFARequiredError, MFAState, save_cookies
from parentsquare_mcp.config import BASE_URL, DEFAULT_TIMEOUT, URLS
from parentsquare_mcp.errors import SessionExpired, raise_for_known_errors

logger = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# A logged-out page still renders gon, with the user explicitly null.
_GON_NULL_RE = re.compile(r"gon\.user_id\s*=\s*null\b")


class TimeoutSession(requests.Session):
    """A ``requests.Session`` whose requests time out unless told otherwise.

    ``requests`` has no default timeout, so one stalled read used to hang a
    caller forever (W7). ``PSClient`` passes its own timeout on every call as
    well; this default also covers code that uses the session directly.
    """

    def __init__(self, timeout: Any = DEFAULT_TIMEOUT):
        super().__init__()
        self.timeout = timeout

    def request(self, method, url, **kwargs):  # type: ignore[override]
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().request(method, url, **kwargs)


def make_session(timeout: Any = DEFAULT_TIMEOUT) -> requests.Session:
    """Build a requests session that identifies itself as a browser.

    ParentSquare rejects non-browser clients with a 403 ``browser_unsupported``
    page, so the ``User-Agent`` must contain "Chrome". This lives here, as
    ``PSClient``'s default session, rather than at the call site: a bare
    ``PSClient()`` would otherwise send ``python-requests/x.y`` and silently get
    unauthenticated content back even with valid cookies. Pinned by
    tests/test_user_agent.py. Every request also gets *timeout* by default.
    """
    session = TimeoutSession(timeout)
    session.headers.update(BROWSER_HEADERS)
    return session


class _EnvCredentials:
    """Sentinel: log in again with ``auth.load_credentials()`` (env, then 1Password/LastPass)."""

    def __repr__(self) -> str:
        return "ENV_CREDENTIALS"


ENV_CREDENTIALS: Any = _EnvCredentials()


@dataclass
class AccountInfo:
    """Discovered account info: user, schools, and students."""

    user_id: int = 0
    schools: dict[int, str] = field(default_factory=dict)  # {school_id: name}
    students: dict[int, dict] = field(default_factory=dict)  # {student_id: {name, school_id, grade}}
    time_zones: dict[int, str] = field(default_factory=dict)  # {school_id: Rails zone name}


@dataclass
class PSClient:
    """HTTP client wrapper for ParentSquare with auto re-login on session expiry.

    Every argument has a default that suits the MCP server; a program running
    several accounts in one process passes them explicitly:

    * ``timeout`` — (connect, read) seconds applied to every request (W7).
    * ``cookie_path`` / ``mfa_state_path`` — where this client keeps its cookies
      and pending MFA state. ``None`` means ``auth.COOKIE_FILE`` /
      ``auth.MFA_STATE_FILE`` as they are at the time of each write (W9).
    * ``credentials`` — how to log in again when the session expires:
      ``ENV_CREDENTIALS`` (default: env vars, then 1Password/LastPass), an
      ``(email, password)`` tuple, a callable returning one, or ``None`` to never
      log in again and raise ``SessionExpired`` instead (W9).
    """

    session: requests.Session = field(default_factory=make_session)
    mfa_state: MFAState | None = None
    account: AccountInfo = field(default_factory=AccountInfo)
    _csrf_token: str | None = field(default=None, repr=False)
    timeout: Any = DEFAULT_TIMEOUT
    cookie_path: Path | None = None
    mfa_state_path: Path | None = None
    credentials: Any = field(default=ENV_CREDENTIALS, repr=False)
    _saved_jar: list | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Session and cookies
    # ------------------------------------------------------------------

    def _auth_kwargs(self) -> dict:
        """Paths and timeout for ``auth.login``/``submit_mfa`` — only the non-default ones.

        A default client calls them exactly as 0.4.0 did, so code that stubs
        ``auth.login`` with a three-argument function keeps working.
        """
        kwargs: dict[str, Any] = {}
        if self.cookie_path is not None:
            kwargs["cookie_path"] = self.cookie_path
        if self.mfa_state_path is not None:
            kwargs["mfa_state_path"] = self.mfa_state_path
        if self.timeout != DEFAULT_TIMEOUT:
            kwargs["timeout"] = self.timeout
        return kwargs

    def _relogin(self) -> None:
        """Log in again with this client's credentials.

        Raises ``SessionExpired`` when the client was built with
        ``credentials=None``. Raises MFARequiredError if 2FA is needed — caller
        should store the mfa_state and prompt the user for the code via
        submit_mfa_code tool.
        """
        self.invalidate_csrf_token()
        creds = self.credentials
        if creds is None:
            raise SessionExpired(
                "ParentSquare session expired (redirected to signin) and this client was built with "
                "credentials=None, so it will not log in again. Reconnect the account."
            )
        logger.info("Session expired, loading credentials...")
        if creds is ENV_CREDENTIALS:
            email, password = auth.load_credentials()
        elif callable(creds):
            email, password = creds()
        else:
            email, password = creds
        try:
            auth.login(self.session, email, password, **self._auth_kwargs())
        except MFARequiredError as exc:
            self.mfa_state = exc.mfa_state
            raise
        self._saved_jar = self._jar_state()

    def login(self, email: str, password: str) -> None:
        """Log in with these credentials, saving cookies to this client's paths.

        Raises MFARequiredError (with ``self.mfa_state`` set) when a code is
        needed; finish with ``submit_mfa(code)``.
        """
        self.invalidate_csrf_token()
        try:
            auth.login(self.session, email, password, **self._auth_kwargs())
        except MFARequiredError as exc:
            self.mfa_state = exc.mfa_state
            raise
        self._saved_jar = self._jar_state()

    def submit_mfa(self, code: str) -> None:
        """Finish an MFA login started by ``login`` or a relogin."""
        state = self.mfa_state or MFAState.load(self.mfa_state_path)
        if state is None:
            raise RuntimeError("No pending MFA verification for this client.")
        auth.submit_mfa(self.session, state, code, **self._auth_kwargs())
        self.mfa_state = None
        self.invalidate_csrf_token()
        self._saved_jar = self._jar_state()

    def load_cookies(self) -> bool:
        """Load this client's saved cookies. Returns True if any were loaded."""
        loaded = auth.load_cookies(self.session, self.cookie_path)
        if loaded:
            self._saved_jar = self._jar_state()
        return loaded

    def _jar_state(self) -> list:
        return sorted(
            (c.name, c.value, c.domain, c.path, bool(c.secure), c.expires if c.expires is not None else -1)
            for c in self.session.cookies
        )

    def _save_cookies_if_changed(self) -> None:
        """Persist the cookie jar, but only when it differs from what was last saved.

        ``ps_s`` rotates, so the jar has to be written back to survive a
        restart; an unchanged jar is not rewritten (W10). The write itself is
        atomic and mode 600 (``auth.write_private``).
        """
        try:
            state = self._jar_state()
            if state == self._saved_jar:
                return
            save_cookies(self.session, self.cookie_path)
            self._saved_jar = state
        except Exception:
            logger.debug("Failed to save cookies (non-fatal)", exc_info=True)

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_logged_out(resp, html: bool = False, api: bool = False) -> bool:
        """Did this response come back signed out?

        A bounce to /signin is the usual sign (checked live: the feed and chats
        pages 302 there without a session cookie). An HTML page that renders
        ``gon.user_id=null`` is a 200 without auth, e.g. the root page (W11).
        A JSON API answers 401.
        """
        if "/signin" in (getattr(resp, "url", "") or ""):
            return True
        if html and _GON_NULL_RE.search(getattr(resp, "text", "") or ""):
            return True
        return api and getattr(resp, "status_code", 200) == 401

    def _get(self, url: str, *, params: dict | None = None, headers: dict | None = None,
             html: bool = False, api: bool = False, what: str = ""):
        """GET with typed errors, one re-login on expiry, and a cookie save."""
        kwargs: dict[str, Any] = {"params": params, "timeout": self.timeout}
        if headers is not None:
            kwargs["headers"] = headers
        resp = self.session.get(url, **kwargs)
        raise_for_known_errors(resp)
        if self._looks_logged_out(resp, html, api):
            self._relogin()
            resp = self.session.get(url, **kwargs)
            raise_for_known_errors(resp)
            if self._looks_logged_out(resp, html, api):
                raise SessionExpired(
                    f"ParentSquare still answers signed out after logging in again ({what or url})."
                )
        resp.raise_for_status()
        self._save_cookies_if_changed()
        return resp

    def get_page(self, path: str, params: dict | None = None) -> BeautifulSoup:
        """GET a page and return parsed BeautifulSoup.

        Automatically re-authenticates if redirected to /signin or served a
        logged-out page; raises ``SessionExpired`` if that does not help.
        """
        resp = self._get(f"{BASE_URL}{path}", params=params, html=True, what=path)
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

        page_resp = self.session.get(f"{BASE_URL}/", timeout=self.timeout)
        raise_for_known_errors(page_resp)
        if self._looks_logged_out(page_resp, html=True):
            self._relogin()
            page_resp = self.session.get(f"{BASE_URL}/", timeout=self.timeout)

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
        raise_for_known_errors(resp)
        if self._is_csrf_rejection(resp):
            logger.info("CSRF token rejected, refetching and retrying once")
            resp = send(self._get_csrf_token(force_refresh=True))
            raise_for_known_errors(resp)
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
                timeout=self.timeout,
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
                timeout=self.timeout,
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
                timeout=self.timeout,
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
                timeout=self.timeout,
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
                data={"utf8": "✓", "authenticity_token": csrf_token, **data},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Accept": "*/*;q=0.5, text/javascript, application/javascript, "
                    "application/ecmascript, application/x-ecmascript",
                    "X-CSRF-Token": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": BASE_URL,
                },
                timeout=self.timeout,
            )
        )
        self._save_cookies_if_changed()
        return resp

    def get_text(self, path: str, params: dict | None = None) -> str:
        """GET a path and return the raw response text (e.g. an edit-form JS body).

        Automatically re-authenticates if redirected to /signin.
        """
        headers = {"Accept": "text/javascript", "X-Requested-With": "XMLHttpRequest"}
        return self._get(f"{BASE_URL}{path}", params=params, headers=headers, what=path).text

    def get_html(self, path: str, params: dict | None = None) -> str:
        """GET an HTML admin page and return the raw response text.

        Distinct from ``get_text``: this sends the session's normal browser
        ``Accept`` header rather than ``text/javascript`` + ``X-Requested-With``.
        Rails ``respond_to`` picks a format from those headers, so asking for JS
        on a plain HTML page yields a template-less 404 — the same trap
        documented on ``get_json``.
        """
        return self._get(f"{BASE_URL}{path}", params=params, html=True, what=path).text

    def get_json(self, path: str, params: dict | None = None) -> dict:
        """GET a JSON API endpoint and return parsed response.

        Automatically re-authenticates if redirected to /signin (or on a 401).
        """
        # Accept must stay JSON-only: the Rails roster feeds (e.g.
        # /schools/{id}/roster/parents_data) 404 if text/javascript is offered,
        # because respond_to then picks the JS format, which has no template.
        # Pinned by tests/test_accept_headers.py.
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        return self._get(f"{BASE_URL}{path}", params=params, headers=headers, api=True, what=path).json()

    def get_raw(self, url: str, stream: bool = False) -> requests.Response:
        """GET a raw URL (for S3/CloudFront downloads). No base URL prepended."""
        resp = self.session.get(url, stream=stream, timeout=self.timeout)
        raise_for_known_errors(resp)
        resp.raise_for_status()
        return resp

    def get_ics(self, path: str) -> str:
        """GET an ICS calendar endpoint and return raw text."""
        return self._get(f"{BASE_URL}{path}", headers={"Accept": "text/calendar"}, what=path).text

    # ------------------------------------------------------------------
    # Account discovery
    # ------------------------------------------------------------------

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

    def _school_record(self, school_id: int) -> str | None:
        """Fetch /api/v2/schools/{id}: remember its time zone and return its name."""
        data = self.get_json(f"/api/v2/schools/{school_id}")
        attrs = data["data"]["attributes"]
        if attrs.get("time_zone"):
            self.account.time_zones[school_id] = attrs["time_zone"]
        return attrs.get("name")

    def school_tz(self, school_id: int) -> tzinfo | None:
        """The school's time zone (chat and notice times are shown in it), or None."""
        from parentsquare_mcp.parsers.dates import resolve_tz

        if school_id not in self.account.time_zones:
            try:
                self._school_record(school_id)
            except Exception:
                logger.debug("Could not load time zone for school %s", school_id, exc_info=True)
                return None
        return resolve_tz(self.account.time_zones.get(school_id))

    def _student_school_id(self, student_id: int, school_name: str) -> int:
        """Match a sidebar student ("1st Grade • School Name") to a school id.

        The sidebar carries no school id, so match the name exactly first, then a
        single unambiguous substring, then an account with only one school. When
        none of those settles it, read the student's dashboard, whose
        ``gon.institute_id`` is the student's school (W17). Returns 0 only when
        even that fails, and logs it.
        """
        wanted = " ".join(school_name.lower().split())
        names = {sid: " ".join(name.lower().split()) for sid, name in self.account.schools.items()}
        exact = [sid for sid, name in names.items() if wanted and name == wanted]
        if len(exact) == 1:
            return exact[0]
        partial = [sid for sid, name in names.items() if wanted and (wanted in name or name in wanted)]
        if len(partial) == 1:
            return partial[0]
        if len(self.account.schools) == 1 and not wanted:
            return next(iter(self.account.schools))
        try:
            soup = self.get_page(URLS["student_dashboard"].format(student_id=student_id))
            _, institute_id, institute_type = self._extract_gon(soup)
            if institute_id and institute_type.lower() in ("school", ""):
                if institute_id not in self.account.schools:
                    try:
                        self.account.schools[institute_id] = self._school_record(institute_id) or school_name
                    except Exception:
                        self.account.schools[institute_id] = school_name or f"School {institute_id}"
                return institute_id
        except Exception:
            logger.debug("Could not read dashboard for student %s", student_id, exc_info=True)
        if len(self.account.schools) == 1:
            return next(iter(self.account.schools))
        logger.warning("Could not match student %s (school %r) to a school id", student_id, school_name)
        return 0

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

        # Get current school name (and time zone) from the API
        try:
            current_name = self._school_record(current_school_id) or f"School {current_school_id}"
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
                                    self.account.schools[sid] = self._school_record(sid) or a.get_text(strip=True)
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
            if student_id in self.account.students:
                continue
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
            self.account.students[student_id] = {
                "name": name,
                "school_id": self._student_school_id(student_id, school_name),
                "grade": grade,
            }

        logger.info(
            f"Discovered account: user_id={self.account.user_id}, "
            f"{len(self.account.schools)} schools, {len(self.account.students)} students"
        )
        return self.account

    # ------------------------------------------------------------------
    # Structured data
    # ------------------------------------------------------------------

    def list_groups(self, school_id: int) -> list:
        """The groups visible at a school, via the GraphQL API the groups page uses.

        The groups page's HTML is an empty React shell, so ``parse_groups_list``
        cannot read it (it raises ``ParseDriftError``). Post counts come from
        ``/api/v2/schools/{id}/groups`` when that answers.
        """
        from parentsquare_mcp.parsers.groups import GROUPS_QUERY, groups_from_graphql

        variables = {"institute": {"type": "school", "id": school_id}, "studentId": None}
        data = self.graphql(GROUPS_QUERY, variables, "GetGroups")
        post_counts: dict[int, int] = {}
        try:
            jg = self.get_json(f"/api/v2/schools/{school_id}/groups")
            for item in jg.get("data", []):
                attrs = item.get("attributes", {})
                gid = attrs.get("id") or item.get("id")
                if gid is not None:
                    post_counts[int(gid)] = attrs.get("active_posts_count", 0)
        except Exception:
            logger.debug("Could not enrich group post counts from JSON:API", exc_info=True)
        return groups_from_graphql(data, post_counts)
