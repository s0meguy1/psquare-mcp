"""W7: every HTTP call times out instead of waiting forever.

A local server accepts connections and never answers. Each client, auth and
download call must raise ``requests.Timeout`` within the configured limit —
before this, no call passed ``timeout`` and a stalled read hung the caller
until something killed the process (on the pstriage server, after an hour,
possibly mid cookie-write).
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
import requests

from parentsquare_mcp import attachments, auth
from parentsquare_mcp import client as client_mod
from parentsquare_mcp.auth import MFAState
from parentsquare_mcp.client import PSClient, make_session

LIMIT = (0.5, 0.3)   # connect, read
MAX_WAIT = 3.0       # generous wall-clock bound per call


@pytest.fixture
def black_hole(monkeypatch):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    srv.settimeout(0.05)
    conns: list[socket.socket] = []
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conns.append(conn)  # read nothing, answer nothing
            except (TimeoutError, OSError):
                pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.getsockname()[1]}"
    monkeypatch.setattr(client_mod, "BASE_URL", base)
    monkeypatch.setattr(auth, "BASE_URL", base)
    yield base
    stop.set()
    thread.join(timeout=2)
    for conn in conns:
        conn.close()
    srv.close()


def _times_out(call) -> None:
    started = time.monotonic()
    with pytest.raises(requests.Timeout):
        call()
    assert time.monotonic() - started < MAX_WAIT


@pytest.fixture
def client(black_hole, tmp_path):
    c = PSClient(timeout=LIMIT, credentials=None, cookie_path=tmp_path / "c.json")
    c._csrf_token = "tok"  # so writes go straight to their own request
    return c


@pytest.mark.parametrize("call", [
    lambda c, base: c.get_page("/schools/1/feeds"),
    lambda c, base: c.get_json("/api/v2/schools/1"),
    lambda c, base: c.get_ics("/schools/1/calendars.ics"),
    lambda c, base: c.get_text("/x"),
    lambda c, base: c.get_html("/x"),
    lambda c, base: c.get_raw(f"{base}/file.pdf"),
    lambda c, base: c.graphql("query Q { x }", {}, "Q"),
    lambda c, base: c.post_json("/api/v2/x", {}),
    lambda c, base: c.post_form("/x", {}),
    lambda c, base: c.send_json("PUT", "/api/v2/x", {}),
    lambda c, base: c.post_json_raw("/x", {}),
    lambda c, base: c._get_csrf_token(force_refresh=True),
], ids=["page", "json", "ics", "text", "html", "raw", "graphql", "post_json", "post_form", "send_json",
        "post_json_raw", "csrf"])
def test_client_calls_time_out(client, black_hole, call):
    _times_out(lambda: call(client, black_hole))


def test_login_times_out(black_hole):
    _times_out(lambda: auth.login(make_session(), "a@example.com", "pw", timeout=LIMIT))


def test_mfa_submit_times_out(black_hole, tmp_path):
    state = MFAState(contact_value="a***@example.com", contact_method="email", email="a@example.com")
    _times_out(lambda: auth.submit_mfa(make_session(), state, "123456", timeout=LIMIT,
                                       cookie_path=tmp_path / "c.json", mfa_state_path=tmp_path / "m.json"))


def test_session_check_times_out(black_hole):
    _times_out(lambda: auth.is_session_valid(make_session(), timeout=LIMIT))


def test_attachment_download_times_out(client, black_hole):
    _times_out(lambda: attachments.fetch(client, f"{black_hole}/big.pdf", max_bytes=1024))


def test_session_has_a_default_timeout_for_direct_use(black_hole):
    """Code that uses the session directly (not through PSClient) is covered too."""
    session = make_session(timeout=LIMIT)
    _times_out(lambda: session.get(f"{black_hole}/anything"))


def test_default_client_timeout_is_set():
    assert PSClient().timeout == (10, 30)
    assert make_session().timeout == (10, 30)
