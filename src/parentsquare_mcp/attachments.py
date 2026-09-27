"""Fetch post and message attachments without the MCP server (W12).

The server used to download a whole file into memory and only then compare its
size to the cap. ``fetch`` streams instead: it refuses up front when
``Content-Length`` is over the cap and stops reading the moment the running
total passes it, so an oversized file costs at most one chunk past the cap.

    from parentsquare_mcp.attachments import fetch, fetch_pdf_text
    img = fetch(client, attachment.url, max_bytes=5 * 1024 * 1024)
    img.data, img.content_type                  # bytes, "image/png"
    text = fetch_pdf_text(client, pdf.url)      # None if too big/unreadable

*source* is a ``PSClient`` (its session, timeout and typed errors are used) or a
plain ``requests.Session``. PDF text needs the optional ``pdf`` extra (pymupdf).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from parentsquare_mcp.config import DEFAULT_TIMEOUT
from parentsquare_mcp.errors import ParentSquareError, raise_for_known_errors
from parentsquare_mcp.urls import redact_url

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


class AttachmentTooLarge(ParentSquareError):
    """The file is bigger than the cap; nothing past the cap (plus one chunk) was read."""

    def __init__(self, url: str, max_bytes: int, seen: int | None = None):
        size = f"{seen} bytes" if seen is not None else "more"
        super().__init__(f"attachment {redact_url(url)} is {size}, over the {max_bytes}-byte cap")
        self.max_bytes = max_bytes
        self.seen = seen


class AttachmentNotAllowed(ParentSquareError):
    """The URL failed the caller's ``allow`` check, so it was not fetched."""


@dataclass
class Download:
    url: str
    data: bytes
    content_type: str  # e.g. "image/png", without parameters

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def subtype(self) -> str:
        """"png" for "image/png" — what MCP's Image(format=...) wants."""
        return self.content_type.split("/")[-1] or "octet-stream"


def _open(source: Any, url: str, timeout: Any):
    if hasattr(source, "get_raw"):  # a PSClient: its timeout and typed errors
        return source.get_raw(url, stream=True)
    resp = source.get(url, stream=True, timeout=timeout or DEFAULT_TIMEOUT)
    raise_for_known_errors(resp)
    resp.raise_for_status()
    return resp


def fetch(source: Any, url: str, *, max_bytes: int, timeout: Any = None,
          allow: Callable[[str], bool] | None = None) -> Download:
    """Download *url* into memory, never reading much past *max_bytes*.

    Raises ``AttachmentTooLarge`` (declared or actual size over the cap),
    ``AttachmentNotAllowed`` (when *allow* rejects the URL), or the request's
    own errors (``requests.Timeout``, ``HTTPError``, typed ParentSquare errors).
    """
    if allow is not None and not allow(url):
        raise AttachmentNotAllowed(f"refusing to fetch {redact_url(url)}")
    resp = _open(source, url, timeout)
    try:
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise AttachmentTooLarge(url, max_bytes, int(declared))
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=CHUNK_BYTES):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise AttachmentTooLarge(url, max_bytes, None)
            chunks.append(chunk)
        content_type = (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
        return Download(url=url, data=b"".join(chunks), content_type=content_type)
    finally:
        resp.close()


def pdf_text(data: bytes) -> str | None:
    """Text of a PDF, pages separated by ``---``; None without pymupdf or text."""
    try:
        import fitz  # pymupdf, the optional "pdf" extra
    except ImportError:
        logger.debug("pymupdf is not installed; install parentsquare-mcp[pdf] for PDF text")
        return None
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        pages = [text for page in doc if (text := page.get_text().strip())]
    finally:
        doc.close()
    return "\n\n---\n\n".join(pages) if pages else None


def fetch_image(source: Any, url: str, *, max_bytes: int = MAX_IMAGE_BYTES, timeout: Any = None,
                allow: Callable[[str], bool] | None = None) -> Download | None:
    """``fetch`` for an image, returning None (and logging) instead of raising."""
    try:
        return fetch(source, url, max_bytes=max_bytes, timeout=timeout, allow=allow)
    except Exception:
        logger.debug(f"Failed to fetch image: {redact_url(url)}", exc_info=True)
        return None


def fetch_pdf_text(source: Any, url: str, *, max_bytes: int = MAX_PDF_BYTES, timeout: Any = None,
                   allow: Callable[[str], bool] | None = None) -> str | None:
    """Download a PDF (streamed, capped) and return its text, or None."""
    try:
        return pdf_text(fetch(source, url, max_bytes=max_bytes, timeout=timeout, allow=allow).data)
    except Exception:
        logger.debug(f"Failed to extract PDF text: {redact_url(url)}", exc_info=True)
        return None
