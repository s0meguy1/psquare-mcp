"""W12: attachment downloads stream with a byte cap, outside the MCP server.

0.4.0 kept this logic private to server.py and downloaded the whole file before
checking its size. ``parentsquare_mcp.attachments.fetch`` refuses up front when
Content-Length is over the cap, and otherwise stops reading one chunk past it.
"""

from __future__ import annotations

import pytest
import requests

from parentsquare_mcp import attachments
from parentsquare_mcp.attachments import AttachmentNotAllowed, AttachmentTooLarge, fetch
from parentsquare_mcp.client import PSClient

CHUNK = attachments.CHUNK_BYTES


class StreamingResp:
    def __init__(self, chunks: int, content_length: bool = False, ctype="image/png; charset=binary", status=200):
        self.status_code = status
        self.url = "https://posts.parentsquare.com/x.png"
        self.text = ""
        self.total_chunks = chunks
        self.consumed = 0
        self.closed = False
        self.headers = {"Content-Type": ctype}
        if content_length:
            self.headers["Content-Length"] = str(chunks * CHUNK)

    def iter_content(self, chunk_size):
        assert chunk_size == CHUNK
        for _ in range(self.total_chunks):
            self.consumed += 1
            yield b"x" * CHUNK

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def close(self):
        self.closed = True


class Session:
    def __init__(self, resp):
        self.resp = resp
        self.gets = 0
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def get(self, url, stream=False, timeout=None, **kw):
        self.gets += 1
        assert stream is True and timeout is not None
        return self.resp


def test_oversized_file_stops_at_the_cap():
    resp = StreamingResp(chunks=1000)  # ~64 MB, no Content-Length
    with pytest.raises(AttachmentTooLarge):
        fetch(Session(resp), "https://posts.parentsquare.com/x.png", max_bytes=3 * CHUNK)
    assert resp.consumed == 4  # three chunks fit; the fourth crosses the cap
    assert resp.closed


def test_declared_size_over_the_cap_downloads_nothing():
    resp = StreamingResp(chunks=1000, content_length=True)
    with pytest.raises(AttachmentTooLarge):
        fetch(Session(resp), "https://posts.parentsquare.com/x.png", max_bytes=3 * CHUNK)
    assert resp.consumed == 0 and resp.closed


def test_small_file_returns_bytes_and_type():
    resp = StreamingResp(chunks=2)
    got = fetch(Session(resp), "https://posts.parentsquare.com/x.png", max_bytes=3 * CHUNK)
    assert got.size == 2 * CHUNK and got.data == b"x" * (2 * CHUNK)
    assert got.content_type == "image/png" and got.subtype == "png"
    assert resp.closed


def test_through_a_psclient_uses_its_session_and_timeout():
    resp = StreamingResp(chunks=1)
    client = PSClient(session=Session(resp), credentials=None)
    assert fetch(client, "https://posts.parentsquare.com/x.png", max_bytes=CHUNK).size == CHUNK


def test_allow_check_runs_before_any_request():
    session = Session(StreamingResp(chunks=1))
    with pytest.raises(AttachmentNotAllowed):
        fetch(session, "http://169.254.169.254/latest", max_bytes=CHUNK, allow=lambda u: u.startswith("https://"))
    assert session.gets == 0


def test_convenience_wrappers_return_none_instead_of_raising():
    assert attachments.fetch_image(Session(StreamingResp(chunks=10)), "https://p/x.png", max_bytes=CHUNK) is None
    assert attachments.fetch_pdf_text(Session(StreamingResp(chunks=10)), "https://p/x.pdf", max_bytes=CHUNK) is None


def test_server_image_helper_uses_the_cap(monkeypatch):
    from parentsquare_mcp import server

    monkeypatch.setattr(server, "_MAX_IMAGE_BYTES", CHUNK)
    client = PSClient(session=Session(StreamingResp(chunks=50)), credentials=None)
    assert server._fetch_image(client, "https://posts.parentsquare.com/x.png") == (None, 0)


def test_pdf_text_extraction():
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Field trip on Friday")
    data = doc.tobytes()
    doc.close()
    assert "Field trip on Friday" in attachments.pdf_text(data)
