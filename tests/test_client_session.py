"""W9, W10, W11: per-client session files and credentials, careful cookie writes,
and logged-out pages detected on every request.

W9  — ``PSClient(cookie_path=..., mfa_state_path=..., credentials=...)`` keeps
      several accounts apart in one process; ``credentials=None`` never logs in
      again and raises ``SessionExpired``. The env-driven defaults (and callers
      that reassign ``auth.COOKIE_FILE``) keep working.
W10 — cookies are written only when the jar changed, atomically, mode 600,
      and logged at DEBUG.
W11 — a 200 page rendering ``gon.user_id=null`` (checked live: that is what the
      feed and chats pages land on without the session cookie) is treated as
      expired, like the /signin bounce.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import requests

from parentsquare_mcp import auth
from parentsquare_mcp import client as client_mod
from parentsquare_mcp.auth import MFAState
from parentsquare_mcp.client import PSClient
from parentsquare_mcp.errors import SessionExpired

BASE = "https://www.parentsquare.com"
FIXTURES = Path(__file__).parent / "fixtures" / "live_2026_09"
LOGGED_OUT = (FIXTURES / "logged_out.html").read_text(encoding="utf-8")
LOGGED_IN = "<html><body><script>gon.user_id=4242;gon.institute_id=7;</script><div id='feeds-list'></div></body></html>"


class Resp:
    def __init__(self, text=LOGGED_IN, url=BASE + "/schools/7/feeds", status=200):
        self.text = text
        self.url = url
        self.status_code = status
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class Session:
    """Serves pages in order; can rotate a cookie on each request like ps_s does."""

    def __init__(self, pages=None, rotate=False, name="ps_s"):
        self.pages = list(pages or [Resp()])
        self.rotate = rotate
        self.name = name
        self.requests = 0
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set(name, "v0", domain=".parentsquare.com", path="/")

    def get(self, url, **kw):
        self.requests += 1
        if self.rotate:
            self.cookies.set(self.name, f"v{self.requests}", domain=".parentsquare.com", path="/")
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]


# --- W9 ----------------------------------------------------------------------------


def test_two_clients_with_their_own_cookie_files(tmp_path, monkeypatch):
    default_file = tmp_path / "default.json"
    monkeypatch.setattr(auth, "COOKIE_FILE", default_file)
    a = PSClient(session=Session(name="family_a"), cookie_path=tmp_path / "a.json", credentials=None)
    b = PSClient(session=Session(name="family_b"), cookie_path=tmp_path / "b.json", credentials=None)
    a.get_page("/schools/7/feeds")
    b.get_page("/schools/7/feeds")
    assert set(json.loads((tmp_path / "a.json").read_text())) == {"family_a"}
    assert set(json.loads((tmp_path / "b.json").read_text())) == {"family_b"}
    assert not default_file.exists()


def test_loading_uses_the_clients_own_file(tmp_path):
    path = tmp_path / "family.json"
    path.write_text(json.dumps({"ps_s": {"value": "abc", "domain": ".parentsquare.com", "path": "/"}}))
    c = PSClient(session=requests.Session(), cookie_path=path, credentials=None)
    assert c.load_cookies() is True
    assert c.session.cookies.get("ps_s") == "abc"


def test_credentials_none_raises_session_expired_instead_of_logging_in(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "login", lambda *a, **k: pytest.fail("must not log in"))
    monkeypatch.setattr(auth, "load_credentials", lambda: pytest.fail("must not read credentials"))
    c = PSClient(session=Session([Resp(url=BASE + "/signin", text=LOGGED_OUT)]), credentials=None,
                 cookie_path=tmp_path / "c.json")
    with pytest.raises(SessionExpired):
        c.get_page("/schools/7/feeds")


@pytest.mark.parametrize("creds", [("parent@example.com", "pw"), lambda: ("parent@example.com", "pw")],
                         ids=["tuple", "callable"])
def test_explicit_credentials_are_used_to_log_in_again(tmp_path, monkeypatch, creds):
    seen = []

    def fake_login(session, email, password, **kw):
        seen.append((email, password, kw.get("cookie_path"), kw.get("mfa_state_path")))

    monkeypatch.setattr(auth, "login", fake_login)
    monkeypatch.setattr(auth, "load_credentials", lambda: pytest.fail("must not read env credentials"))
    session = Session([Resp(url=BASE + "/signin", text=LOGGED_OUT), Resp()])
    c = PSClient(session=session, credentials=creds, cookie_path=tmp_path / "c.json",
                 mfa_state_path=tmp_path / "m.json")
    c.get_page("/schools/7/feeds")
    assert seen == [("parent@example.com", "pw", tmp_path / "c.json", tmp_path / "m.json")]


def test_default_client_still_uses_env_credentials(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "load_credentials", lambda: calls.append("env") or ("u", "p"))
    monkeypatch.setattr(auth, "login", lambda *a: calls.append("login"))
    c = PSClient(session=Session([Resp(url=BASE + "/signin", text=LOGGED_OUT), Resp()]))
    monkeypatch.setattr(c, "_save_cookies_if_changed", lambda: None)
    c.get_page("/schools/7/feeds")
    assert calls == ["env", "login"]


def test_module_cookie_file_can_still_be_reassigned(tmp_path, monkeypatch):
    """pstriage overwrites auth.COOKIE_FILE after import; a default client must honour it."""
    target = tmp_path / "reassigned.json"
    monkeypatch.setattr(auth, "COOKIE_FILE", target)
    PSClient(session=Session(), credentials=None).get_page("/schools/7/feeds")
    assert target.exists()


def test_mfa_state_goes_to_the_clients_file(tmp_path):
    state = MFAState(contact_value="a***@example.com", contact_method="email", email="a@example.com")
    state.save(tmp_path / "m.json")
    assert MFAState.load(tmp_path / "m.json") == state
    MFAState.clear(tmp_path / "m.json")
    assert not (tmp_path / "m.json").exists()


# --- W10 ---------------------------------------------------------------------------


def _count_writes(monkeypatch) -> list:
    writes = []
    real = client_mod.save_cookies

    def counting(session, path=None):
        writes.append(path)
        real(session, path)

    monkeypatch.setattr(client_mod, "save_cookies", counting)
    return writes


def test_unchanged_jar_is_written_once(tmp_path, monkeypatch):
    writes = _count_writes(monkeypatch)
    c = PSClient(session=Session(), cookie_path=tmp_path / "c.json", credentials=None)
    for _ in range(10):
        c.get_page("/schools/7/feeds")
    assert len(writes) == 1


def test_loaded_jar_is_not_rewritten(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"ps_s": {"value": "v0", "domain": ".parentsquare.com", "path": "/"}}))
    writes = _count_writes(monkeypatch)
    session = Session()
    session.cookies.clear()
    c = PSClient(session=session, cookie_path=path, credentials=None)
    c.load_cookies()
    for _ in range(10):
        c.get_page("/schools/7/feeds")
    assert writes == []


def test_a_rotating_cookie_is_written_each_time_it_changes(tmp_path, monkeypatch):
    writes = _count_writes(monkeypatch)
    c = PSClient(session=Session(rotate=True), cookie_path=tmp_path / "c.json", credentials=None)
    for _ in range(3):
        c.get_page("/schools/7/feeds")
    assert len(writes) == 3
    assert json.loads((tmp_path / "c.json").read_text())["ps_s"]["value"] == "v3"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_cookie_and_mfa_files_are_private(tmp_path):
    PSClient(session=Session(), cookie_path=tmp_path / "c.json", credentials=None).get_page("/x")
    MFAState(contact_value="x", contact_method="email", email="e").save(tmp_path / "m.json")
    for name in ("c.json", "m.json"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_an_existing_world_readable_file_becomes_private(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{}")
    path.chmod(0o644)
    auth.save_cookies(Session(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_old_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text('{"ps_s": {"value": "old"}}')

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        auth.save_cookies(Session(), path)
    assert json.loads(path.read_text()) == {"ps_s": {"value": "old"}}
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]  # no temp file left behind


def test_cookie_saves_log_at_debug(tmp_path, caplog):
    with caplog.at_level("INFO", logger="parentsquare_mcp"):
        PSClient(session=Session(rotate=True), cookie_path=tmp_path / "c.json", credentials=None).get_page("/x")
        auth.load_cookies(requests.Session(), tmp_path / "c.json")
    assert "cookies" not in caplog.text.lower()


# --- W11 ---------------------------------------------------------------------------


def test_logged_out_200_page_raises_when_the_client_may_not_log_in(tmp_path):
    assert "gon.user_id=null" in LOGGED_OUT
    c = PSClient(session=Session([Resp(text=LOGGED_OUT)]), credentials=None, cookie_path=tmp_path / "c.json")
    with pytest.raises(SessionExpired):
        c.get_page("/schools/7/feeds")


def test_logged_in_page_does_not_raise(tmp_path):
    c = PSClient(session=Session([Resp()]), credentials=None, cookie_path=tmp_path / "c.json")
    assert c.get_page("/schools/7/feeds").find(id="feeds-list") is not None


def test_logged_out_page_triggers_one_relogin(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    session = Session([Resp(text=LOGGED_OUT), Resp()])
    c = PSClient(session=session, credentials=("u", "p"), cookie_path=tmp_path / "c.json")
    assert c.get_page("/schools/7/feeds").find(id="feeds-list") is not None
    assert session.requests == 2


def test_still_logged_out_after_relogin_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "login", lambda *a, **k: None)
    c = PSClient(session=Session([Resp(text=LOGGED_OUT)]), credentials=("u", "p"), cookie_path=tmp_path / "c.json")
    with pytest.raises(SessionExpired, match="still answers signed out"):
        c.get_page("/schools/7/feeds")


def test_json_401_counts_as_logged_out(tmp_path):
    c = PSClient(session=Session([Resp(status=401, text="{}", url=BASE + "/api/v2/x")]), credentials=None,
                 cookie_path=tmp_path / "c.json")
    with pytest.raises(SessionExpired):
        c.get_json("/api/v2/x")
