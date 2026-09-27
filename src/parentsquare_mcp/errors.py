"""Typed exceptions raised by parentsquare-mcp.

Every class subclasses ``RuntimeError`` — what the library raised before these
existed — so an ``except RuntimeError`` written against 0.4.0 still catches
them. The messages also keep the phrases callers matched on before there were
types to match ("redirected back to signin", "invalid or expired code"), so a
caller can move from substring matching to ``except`` at its own pace.

``MFARequiredError`` is not here: it lives in ``auth`` and deliberately stays an
``Exception`` rather than a ``RuntimeError``, because it is a step in a normal
login, not a failure, and callers already catch it on its own.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class ParentSquareError(RuntimeError):
    """Base class for every error this library raises on purpose."""


class SessionExpired(ParentSquareError):
    """The session is no longer authenticated and this client may not log in again.

    Raised when expiry is detected on a client built with ``credentials=None``,
    or when a fresh login still leaves the page logged out.
    """


class LoginFailed(ParentSquareError):
    """ParentSquare rejected the email and password (it redirected back to /signin)."""


class MFACodeInvalid(ParentSquareError):
    """``/mfa/submit`` answered 401: the code is wrong or has expired."""


class MFANotEstablished(ParentSquareError):
    """The MFA code was accepted, but the session still is not authenticated."""


class BrowserUnsupported(ParentSquareError):
    """ParentSquare answered 403 ``browser_unsupported``.

    It does that when the User-Agent does not look like a browser; see
    ``client.make_session``.
    """


class RateLimited(ParentSquareError):
    """HTTP 429. ``retry_after`` is the server's ``Retry-After`` in seconds, or None."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ParseDriftError(ParentSquareError):
    """A page clearly holds items, but none of them could be parsed.

    This is how markup drift surfaces. Without it a parser that no longer
    understands a page returns ``[]``, which looks exactly like a quiet day.
    ``page`` names the page, ``markers`` is how many items it evidently holds.
    """

    def __init__(self, page: str, message: str, markers: int = 0):
        super().__init__(f"{page}: {message}")
        self.page = page
        self.markers = markers


def _retry_after_seconds(value: str | None) -> float | None:
    """``Retry-After`` is either a number of seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def raise_for_known_errors(resp) -> None:
    """Raise the typed error for a response ParentSquare uses to refuse a client.

    * 429 -> ``RateLimited`` carrying ``Retry-After`` in seconds.
    * 403 naming ``browser_unsupported`` (in the URL or the body) -> ``BrowserUnsupported``.
      Only that exact token counts: every page embeds an i18n string
      "unsupported_browser" for its file uploader, so looser matching would
      misfire on an ordinary 403.

    Anything else is left to the caller (``raise_for_status``, session checks).
    Works on any object with ``status_code``/``url``/``text``/``headers``.
    """
    status = getattr(resp, "status_code", 200)
    if status == 429:
        headers = getattr(resp, "headers", None) or {}
        retry_after = _retry_after_seconds(headers.get("Retry-After"))
        wait = f"; retry after {retry_after:.0f}s" if retry_after is not None else ""
        raise RateLimited(f"ParentSquare rate-limited the request (HTTP 429){wait}", retry_after=retry_after)
    if status == 403:
        url = getattr(resp, "url", "") or ""
        body = (getattr(resp, "text", "") or "")[:20000]
        if "browser_unsupported" in url or "browser_unsupported" in body:
            raise BrowserUnsupported(
                "ParentSquare refused the request as browser_unsupported (HTTP 403): the User-Agent "
                "must look like Chrome. Build the session with parentsquare_mcp.client.make_session()."
            )
