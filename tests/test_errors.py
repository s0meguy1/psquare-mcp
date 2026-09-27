"""W8: each typed exception, raised from its real trigger.

All of them subclass RuntimeError (what 0.4.0 raised), and the ones that
replace a 0.4.0 RuntimeError keep its message, so callers matching on
"redirected back to signin" or "invalid or expired code" keep working while
they move to ``except LoginFailed`` / ``except MFACodeInvalid``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests
from bs4 import BeautifulSoup

from parentsquare_mcp import auth
from parentsquare_mcp.auth import MFAState
from parentsquare_mcp.client import PSClient
from parentsquare_mcp.errors import (
    BrowserUnsupported,
    LoginFailed,
    MFACodeInvalid,
    MFANotEstablished,
    ParentSquareError,
    ParseDriftError,
    RateLimited,
    SessionExpired,
    raise_for_known_errors,
)
from parentsquare_mcp.parsers.feeds import parse_feed_page

BASE = "https://www.parentsquare.com"
SIGNIN_PAGE = '<html><head><meta name="csrf-token" content="tok"></head><body><script>gon.user_id=null;</script></body></html>'


class Resp:
    def __init__(self, status=200, text="", url=BASE + "/", headers=None, payload=None):
        self.status_code = status
        self.text = text
        self.url = url
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Session:
    """Replays canned responses; records every call."""

    def __init__(self, gets=(), posts=()):
        self.gets = list(gets)
        self.posts = list(posts)
        self.calls = []
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self.gets.pop(0) if len(self.gets) > 1 else self.gets[0]

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return self.posts.pop(0) if len(self.posts) > 1 else self.posts[0]


@pytest.mark.parametrize("cls", [SessionExpired, LoginFailed, MFACodeInvalid, MFANotEstablished,
                                 BrowserUnsupported, RateLimited, ParseDriftError])
def test_every_error_is_a_runtime_error(cls):
    assert issubclass(cls, ParentSquareError) and issubclass(cls, RuntimeError)


def test_session_expired_when_the_client_may_not_log_in_again(tmp_path):
    session = Session(gets=[Resp(url=BASE + "/signin", text=SIGNIN_PAGE)])
    client = PSClient(session=session, credentials=None, cookie_path=tmp_path / "c.json")
    with pytest.raises(SessionExpired):
        client.get_page("/schools/1/feeds")


def test_login_failed(tmp_path):
    session = Session(gets=[Resp(url=BASE + "/signin", text=SIGNIN_PAGE)],
                      posts=[Resp(url=BASE + "/signin", text=SIGNIN_PAGE)])
    with pytest.raises(LoginFailed, match="redirected back to signin"):
        auth.login(session, "a@example.com", "wrong", cookie_path=tmp_path / "c.json")


def test_mfa_code_invalid(tmp_path):
    state = MFAState(contact_value="a***@example.com", contact_method="email", email="a@example.com", csrf_token="t")
    session = Session(posts=[Resp(status=401, url=BASE + "/mfa/submit")])
    with pytest.raises(MFACodeInvalid, match="invalid or expired code"):
        auth.submit_mfa(session, state, "000000", cookie_path=tmp_path / "c.json", mfa_state_path=tmp_path / "m.json")


def test_mfa_not_established(tmp_path):
    state = MFAState(contact_value="a***@example.com", contact_method="email", email="a@example.com", csrf_token="t")
    session = Session(gets=[Resp(text=SIGNIN_PAGE)], posts=[Resp(status=200, url=BASE + "/mfa/submit", payload={})])
    with pytest.raises(MFANotEstablished, match="not authenticated"):
        auth.submit_mfa(session, state, "123456", cookie_path=tmp_path / "c.json", mfa_state_path=tmp_path / "m.json")


def test_browser_unsupported_403():
    page = Resp(status=403, url=BASE + "/browser_unsupported", text="<h1>Please use a supported browser</h1>")
    client = PSClient(session=Session(gets=[page]), credentials=None)
    with pytest.raises(BrowserUnsupported):
        client.get_page("/schools/1/feeds")


def test_ordinary_403_is_not_browser_unsupported():
    """Every page embeds an i18n "unsupported_browser" string; that alone must not count."""
    page = Resp(status=403, text='{"dropzone":{"unsupported_browser":"Your browser is not supported."}}')
    client = PSClient(session=Session(gets=[page]), credentials=None)
    with pytest.raises(requests.HTTPError):
        client.get_page("/schools/1/feeds")


def test_rate_limited_carries_retry_after():
    client = PSClient(session=Session(gets=[Resp(status=429, headers={"Retry-After": "30"})]), credentials=None)
    with pytest.raises(RateLimited) as exc:
        client.get_json("/api/v2/schools/1")
    assert exc.value.retry_after == 30.0


def test_rate_limited_http_date():
    with pytest.raises(RateLimited) as exc:
        raise_for_known_errors(SimpleNamespace(status_code=429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
    assert exc.value.retry_after == 0.0  # a date in the past means "now"


def test_rate_limited_on_writes():
    client = PSClient(session=Session(posts=[Resp(status=429)]), credentials=None)
    client._csrf_token = "tok"
    with pytest.raises(RateLimited):
        client.post_form("/x", {})


def test_parse_drift_error():
    html = '<div id="feeds-list"><div class="ps-box"><div id="feed_1"><div class="renamed">?</div></div></div></div>'
    with pytest.raises(ParseDriftError) as exc:
        parse_feed_page(BeautifulSoup(html, "html.parser"), page="feed page 1")
    assert exc.value.page == "feed page 1"
