"""Scrub a captured ParentSquare page into a fixture that is safe to commit.

    uv run python tests/fixtures/scrub.py <raw.html> <out.html> --keep "<css selector>" [--keep ...]

What survives: the markup of the kept subtrees (tags, classes, roles, styles,
layout), ParentSquare's own UI words ("Read More", "Sign Up", month and
weekday names, ...), short numbers (counts, times, dates) and data-timestamps.

What does not: every other word is replaced by a pseudo-word of the same
length and case (so "Community Members,<br>We" keeps its shape), every number
of three or more digits is remapped consistently (ids stay linked across
attributes and URLs), emails, URLs (Google document ids included: they open
the school's real documents), signed-URL credentials, form tokens and
embedded JSON are rewritten, and scripts, comments and the page chrome are
dropped. Mappings come from a keyed hash whose key is random per run and never
stored, so they cannot be reversed.

The upstream repository is public: after scrubbing, grep the output for the
real names, schools and ids you know are in the page before committing it.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

KEY = secrets.token_bytes(32)

UI_WORDS = {
    # dates and times
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august", "september", "october",
    "november", "december", "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "am", "pm",
    "today", "yesterday", "tomorrow", "ago", "days", "day", "hours", "minutes", "at", "to", "by",
    # ParentSquare chrome the parsers read
    "read", "more", "less", "sign", "up", "ups", "closed", "open", "full", "of", "filled",
    "complete", "form", "forms", "download", "file", "files", "pinned", "alert", "alerts",
    "document", "notice", "notices", "messages", "message", "total", "last", "sent",
    "conversation", "with", "comments", "comment", "on", "this", "post", "posts", "private",
    "only", "you", "and", "can", "view", "an", "email", "will", "be", "reply", "delete",
    "appreciate", "print", "note", "no", "action", "required", "deadline", "has", "passed",
    "items", "item", "time", "count", "slot", "appointment", "limited", "per", "user", "across",
    "all", "slots", "input", "fields", "marked", "asterisk", "are", "save", "the", "a", "is",
    "rsvp", "add", "date", "calendar", "google", "outlook", "ical", "other", "opens", "in", "new",
    "tab", "translation", "powered", "translate", "see", "original", "text", "replies",
    "cancel", "remove", "yours", "your", "votes", "vote", "voted", "already", "poll", "polls",
    "https", "http", "www", "com", "org", "net", "edu", "us", "pdf", "png", "jpg", "jpeg", "gif",
    "select", "child", "student", "name", "grade", "class", "classes", "teacher", "contacts",
    "info", "kindergarten", "posted", "profile", "icon", "or", "for", "from",
    # the site glues these in aria-labels ("6:30 PMTo 8:00 PM"); the parser relies on it
    "pmto", "amto",
}

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_LONG_NUM_RE = re.compile(r"\d{3,}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CONSONANTS = "bcdfghjklmnprstvwz"
_VOWELS = "aeiou"


def _digest(kind: str, value: str) -> bytes:
    return hmac.new(KEY, f"{kind}:{value}".encode(), hashlib.sha256).digest()


def pseudo_word(word: str) -> str:
    if word.lower() in UI_WORDS:
        return word
    h = _digest("w", word.lower())
    out = []
    for i in range(len(word)):
        pool = _CONSONANTS if i % 2 == 0 else _VOWELS
        out.append(pool[h[i % len(h)] % len(pool)])
    fake = "".join(out)
    if word.isupper() and len(word) > 1:
        return fake.upper()
    if word[0].isupper():
        return fake[0].upper() + fake[1:]
    return fake


def remap_number(num: str) -> str:
    if len(num) == 4 and 1990 <= int(num) <= 2035:
        return num  # a year
    h = _digest("n", num)
    digits = "".join(str(b % 10) for b in h)[: len(num)]
    if num[0] != "0" and digits[0] == "0":
        digits = "7" + digits[1:]
    return digits


def scrub_numbers(value: str) -> str:
    return _LONG_NUM_RE.sub(lambda m: remap_number(m.group(0)), value)


def scrub_text(value: str) -> str:
    value = _EMAIL_RE.sub(lambda m: f"user{remap_number(str(len(m.group(0)) * 97))}@example.com", value)
    value = scrub_numbers(value)
    return _WORD_RE.sub(lambda m: pseudo_word(m.group(0)), value)


_HEX_RE = re.compile(r"[0-9a-f]{12,}", re.I)
_UPLOAD_PREFIX_RE = re.compile(r"^(inline_images_)([0-9a-f]+_)?(\d+-?)", re.I)
_SIZE_PREFIXES = ("thumb_", "full_", "medium_", "large_", "original_", "small_")
_RANDOM_PREFIX_RE = re.compile(r"^([A-Za-z0-9]{16,})_")
_SAFE = "-_.~+%()'!*,;=@:"


def fake_hex(value: str) -> str:
    h = _digest("h", value.lower()).hex()
    while len(h) < len(value):
        h += _digest("h", h).hex()
    return h[: len(value)]


def fake_token(value: str) -> str:
    """A document id's stand-in: same length and character classes, from the keyed hash."""
    h = _digest("t", value)
    while len(h) < len(value):
        h += _digest("t", h.hex())
    out = []
    for ch, b in zip(value, h):
        if ch.isdigit():
            out.append(str(b % 10))
        elif ch.isupper():
            out.append(chr(ord("A") + b % 26))
        elif ch.islower():
            out.append(chr(ord("a") + b % 26))
        else:
            out.append(ch)  # "-" and "_" keep their places
    return "".join(out)


_GOOGLE_ROUTE_WORDS = {
    "forms", "d", "e", "u", "viewform", "formResponse", "spreadsheets", "document", "presentation",
    "file", "files", "folders", "drive", "view", "edit", "preview", "copy", "pub", "pubhtml",
    "htmlview", "embed", "open", "calendar", "event", "render", "r", "0", "1", "2", "3",
}
_GOOGLE_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,}")


def _scrub_google_path(path: str) -> str:
    """A Google Docs, Drive, Forms or Calendar path: route words kept, document ids faked.

    The ids are live links to a school's own documents (a sign-up sheet can be
    editable by anyone who has the link), so none may survive into a fixture.
    """
    out = []
    for seg in path.split("/"):
        seg = unquote(seg)
        if not seg or seg in _GOOGLE_ROUTE_WORDS:
            out.append(seg)
        elif _GOOGLE_ID_RE.fullmatch(seg):
            out.append(fake_token(seg))
        else:
            out.append(quote(scrub_text(seg), safe=_SAFE))
    return "/".join(out)


def _scrub_words_path(path: str) -> str:
    """An external URL path: every segment scrubbed like text."""
    parts = []
    for seg in path.split("/"):
        seg = unquote(seg)
        stem, dot, ext = seg.rpartition(".")
        if dot and ext.isalnum() and len(ext) <= 5 and stem:
            seg = scrub_text(stem) + "." + ext
        else:
            seg = scrub_text(seg)
        parts.append(quote(seg, safe=_SAFE))
    return "/".join(parts)


def scrub_filename(name: str) -> str:
    """An uploaded file's name: routing prefixes kept, hashes faked, the human part scrubbed."""
    prefix = ""
    m = _UPLOAD_PREFIX_RE.match(name)
    if m:
        hexpart = m.group(2) or ""
        prefix = m.group(1) + (fake_hex(hexpart[:-1]) + "_" if hexpart else "") + m.group(3)
        name = name[m.end():]
    else:
        for size in _SIZE_PREFIXES:
            if name.startswith(size):
                prefix, name = size, name[len(size):]
                break
        m = _RANDOM_PREFIX_RE.match(name)
        if m:
            prefix += fake_hex(m.group(1)) if _HEX_RE.fullmatch(m.group(1)) else scrub_text(m.group(1))
            prefix += "_"
            name = name[m.end():]
    stem, dot, ext = name.rpartition(".")
    if not dot or not ext.isalnum() or len(ext) > 5:
        stem, ext = name, ""
    stem = fake_hex(stem) if _HEX_RE.fullmatch(stem or "-") else scrub_text(stem)
    return prefix + stem + (f".{ext}" if ext else "")


def _scrub_route_path(path: str, upload: bool) -> str:
    """A ParentSquare (or its S3/CDN) path: route words kept, ids remapped.

    On upload hosts the last segment is a user's file name and is scrubbed.
    """
    segs = path.split("/")
    out = []
    for i, seg in enumerate(segs):
        seg = unquote(seg)
        if upload and i == len(segs) - 1 and seg:
            seg = scrub_filename(seg)
        else:
            seg = scrub_numbers(seg)
        out.append(quote(seg, safe=_SAFE))
    return "/".join(out)


_SECRET_PARAMS = re.compile(r"^(x-amz-.*|signature|key-pair-id|expires|policy|token|auth.*|sig)$", re.I)
_VERBATIM_PARAMS = {"dates", "action", "lang", "media", "html", "tab", "role"}


def _is_ps_host(host: str) -> bool:
    return not host or host == "parentsquare.com" or host.endswith(".parentsquare.com")


def scrub_url(url: str) -> str:
    url = url.strip()
    low = url.lower()
    if not url or url.startswith("#"):
        return scrub_numbers(url)
    if low.startswith("javascript:"):
        return url
    if low.startswith("mailto:"):
        return "mailto:user@example.com"
    if low.startswith("tel:"):
        return "tel:5550100"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "https://example.com/"
    host = (parts.hostname or "").lower()
    if host == "fonts.gstatic.com" or host.startswith("assets."):
        return url  # emoji glyphs and the site's own static assets are public
    ps_route = host in ("", "parentsquare.com", "www.parentsquare.com")
    upload = (_is_ps_host(host) and not ps_route) or host.endswith("amazonaws.com") or host.endswith("cloudfront.net")
    google = host.endswith("google.com")
    if ps_route or upload:
        new_host, path = parts.netloc, _scrub_route_path(parts.path, upload)
    elif google:
        new_host, path = parts.netloc, _scrub_google_path(parts.path)
    else:
        labels = host.split(".")
        new_host = ".".join(pseudo_word(label) if i < len(labels) - 1 else label for i, label in enumerate(labels))
        path = _scrub_words_path(parts.path)
    query = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        if _SECRET_PARAMS.match(key):
            val = "x" * min(len(val), 24)
        elif key.lower() == "response-content-disposition":
            val = re.sub(r'filename="?([^";]+)"?', lambda m: 'filename="' + scrub_filename(m.group(1)) + '"', val)
        elif key.lower() in _VERBATIM_PARAMS:
            pass
        elif ps_route or upload:
            val = scrub_numbers(val)
        else:
            val = scrub_text(val)
        query.append((key, val))
    fragment = scrub_numbers(parts.fragment)
    return urlunsplit((parts.scheme, new_host, path, urlencode(query, quote_via=quote), fragment))


def scrub_json(value):
    if isinstance(value, dict):
        return {scrub_numbers(str(k)) if str(k).isdigit() else k: scrub_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_json(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(remap_number(str(value))) if value >= 100 else value
    if isinstance(value, str):
        if value.startswith("#") and len(value) in (4, 7):
            return "#336699"  # a school's theme colours can identify it
        if value in ("school", "district", "groups", "School", "District"):
            return value
        return scrub_text(value)
    return value


KEEP_ATTRS = {
    "class", "role", "type", "rel", "target", "tabindex", "disabled", "hidden", "checked",
    "selected", "colspan", "rowspan", "width", "height", "align", "valign", "border",
    "cellpadding", "cellspacing", "dir", "lang", "method", "autocomplete", "maxlength",
    "data-toggle", "data-remote", "data-method", "data-ps-tab", "data-dismiss", "aria-hidden",
    "aria-expanded", "aria-haspopup", "aria-selected", "aria-level", "aria-live", "aria-modal",
    "data-timestamp", "data-disable-with", "data-read-receipts-v1", "data-readonly", "multiple",
    "data-validate-for", "scope", "start", "reversed", "charset", "http-equiv", "media",
}
ID_ATTRS = {
    "id", "for", "name", "data-testid", "aria-controls", "aria-labelledby", "aria-describedby",
    "data-target", "data-container", "data-mid", "data-feed-id", "data-thread-id", "data-message-id",
    "data-current-user-id", "data-chats-user-id", "data-user", "data-asset-id", "data-id",
    "data-comment-id", "data-chat-id", "data-feed-level",
}
URL_ATTRS = {
    "href", "src", "data-url", "data-fallback-url", "fallback", "data-src", "action",
    "data-remote-template", "data-href", "poster", "background", "data-original",
}
JSON_ATTRS = {"data-feed-levels", "data-recipients", "data-avatar-color-map", "data-app-context"}


def scrub_attr(tag: Tag, name: str, value: str) -> str:
    if name in KEEP_ATTRS:
        return value
    if name == "style":
        return re.sub(r"url\([^)]*\)", "url(https://example.com/bg.png)", value)
    if name in ID_ATTRS:
        return scrub_numbers(value)
    if name in URL_ATTRS:
        return scrub_url(value)
    if name == "srcset":
        return ", ".join(scrub_url(p.strip().split(" ")[0]) for p in value.split(","))
    if name in JSON_ATTRS:
        try:
            return json.dumps(scrub_json(json.loads(value)))
        except ValueError:
            return scrub_text(value)
    if name == "value":
        if tag.get("type") == "hidden" and not value.isdigit():
            return "x" * min(len(value), 22)
        return scrub_text(value)
    if value in ("true", "false", ""):
        return value
    return scrub_text(value)


def scrub_tree(root: Tag) -> None:
    for el in list(root.find_all(["script", "noscript", "iframe", "svg"])):
        el.decompose()
    for c in list(root.find_all(string=lambda s: isinstance(s, Comment))):
        c.extract()
    for el in [root, *root.find_all(True)]:
        el.attrs = {
            name: scrub_attr(el, name, " ".join(v) if isinstance(v, list) and name != "class" else v)
            if name != "class" else [scrub_numbers(c) for c in (v if isinstance(v, list) else v.split())]
            for name, v in el.attrs.items()
        }
    for text in list(root.find_all(string=True)):
        if isinstance(text, NavigableString) and not isinstance(text, Comment):
            new = scrub_text(str(text))
            if new != str(text):
                text.replace_with(NavigableString(new))


def build(raw_html: str, keeps: list[str], title: str = "Fixture", extra_body: str = "",
          stylesheets: bool = True) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    links = []
    if stylesheets:
        for link in soup.find_all("link", rel="stylesheet"):
            href = link.get("href", "")
            if "assets.parentsquare.com" in href:
                links.append(f'<link rel="stylesheet" href="{href}">')
    kept = []
    for sel in keeps:
        for el in soup.select(sel):
            scrub_tree(el)
            kept.append(str(el))
    head = "\n".join(['<meta charset="utf-8">', f"<title>{title}</title>", *links])
    return f"<!DOCTYPE html>\n<html><head>\n{head}\n</head><body>\n" + "\n".join(kept) + f"\n{extra_body}\n</body></html>\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw")
    ap.add_argument("out")
    ap.add_argument("--keep", action="append", required=True, help="CSS selector of a subtree to keep")
    ap.add_argument("--title", default="Fixture")
    args = ap.parse_args()
    with open(args.raw, encoding="utf-8") as f:
        html = build(f.read(), args.keep, args.title)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
