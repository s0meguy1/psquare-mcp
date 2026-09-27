"""Visible text and links, extracted the way a browser's ``innerText`` sees them.

``Tag.get_text(strip=True)`` joins text nodes with nothing between them, so a
post reading "Community Members,<br>We would like" came out as "Community
Members,We would like", and every ``href`` was dropped. ``element_text`` follows
the parts of the innerText algorithm that matter for ParentSquare's
server-rendered HTML:

* whitespace inside a text node collapses to one space (``white-space: normal``),
  except under ``<pre>``/``<textarea>`` or an inline ``white-space: pre*`` style;
* ``<br>`` is a line break, a block element starts and ends a line, and a ``<p>``
  is set off by a blank line;
* table cells are separated by a tab and rows by a line break;
* content the page hides (``display:none``, ``hidden``, Bootstrap's ``hidden`` /
  ``d-none`` / mobile-only classes) and non-rendered tags are skipped;
* images contribute nothing (``innerText`` ignores ``alt``).

The result then has non-breaking spaces turned into plain spaces, spaces trimmed
from both ends of every line, and runs of blank lines collapsed to one. Once
whitespace is normalized it equals the browser's ``innerText`` for the same
element, which tests/test_live_fixtures.py checks against live captures.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import Comment, Declaration, Doctype, NavigableString, ProcessingInstruction, Tag
from bs4.element import CData

from parentsquare_mcp.config import BASE_URL
from parentsquare_mcp.models import Link

# Tags that never render text a reader sees.
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "head", "title", "meta", "link", "base",
    "iframe", "object", "embed", "svg", "canvas", "audio", "video", "map", "select",
    "datalist", "input", "textarea", "img", "picture", "source", "track", "wbr",
})

# Tags whose default display is block-level (one required line break around them).
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "center", "dd", "details", "dialog",
    "dir", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1",
    "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "legend", "li", "main", "menu",
    "nav", "ol", "pre", "section", "summary", "table", "caption", "ul", "optgroup", "option",
})

_NON_TEXT_STRINGS = (Comment, CData, ProcessingInstruction, Declaration, Doctype)

# Bootstrap/utility classes that mean display:none at desktop width.
_HIDDEN_CLASSES = frozenset({"hidden", "d-none", "hide", "invisible", "visible-xs"})
_MOBILE_ONLY = frozenset({"hidden-sm", "hidden-md", "hidden-lg"})

_DISPLAY_RE = re.compile(r"(?:^|;)\s*display\s*:\s*([a-z-]+)", re.I)
_VISIBILITY_RE = re.compile(r"(?:^|;)\s*visibility\s*:\s*(hidden|collapse)", re.I)
_WHITE_SPACE_RE = re.compile(r"(?:^|;)\s*white-space\s*:\s*([a-z-]+)", re.I)
_COLLAPSIBLE_RE = re.compile(r"[ \t\n\r\f]+")
_SPACES_RE = re.compile(r" {2,}")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def is_hidden(tag: Tag) -> bool:
    """True when *tag* would not be rendered at desktop width."""
    if tag.has_attr("hidden"):
        return True
    classes = set(tag.get("class") or ())
    if classes & _HIDDEN_CLASSES or _MOBILE_ONLY <= classes:
        return True
    style = tag.get("style") or ""
    if style:
        m = _DISPLAY_RE.search(style)
        if m and m.group(1).lower() == "none":
            return True
        if _VISIBILITY_RE.search(style):
            return True
    return False


def _display(tag: Tag) -> str:
    """'block', 'p', 'row', 'cell' or 'inline' — the parts of CSS display innerText uses."""
    m = _DISPLAY_RE.search(tag.get("style") or "")
    if m:
        value = m.group(1).lower()
        if value in ("block", "flex", "grid", "table", "list-item", "flow-root", "table-caption"):
            return "p" if tag.name == "p" else "block"
        if value == "table-row":
            return "row"
        if value == "table-cell":
            return "cell"
        return "inline"
    name = tag.name
    if name == "p":
        return "p"
    if name == "tr":
        return "row"
    if name in ("td", "th"):
        return "cell"
    if name in _BLOCK_TAGS:
        return "block"
    return "inline"


def _white_space(tag: Tag, inherited: str) -> str:
    if tag.name in ("pre", "listing", "xmp", "plaintext"):
        return "pre"
    m = _WHITE_SPACE_RE.search(tag.get("style") or "")
    if m:
        value = m.group(1).lower()
        if value in ("pre", "pre-wrap", "break-spaces"):
            return "pre"
        if value == "pre-line":
            return "pre-line"
        if value in ("normal", "nowrap"):
            return "normal"
    return inherited


def _is_last_cell(cell: Tag) -> bool:
    return cell.find_next_sibling(["td", "th"]) is None


def _is_last_row(row: Tag) -> bool:
    if row.find_next_sibling("tr") is not None:
        return False
    group = row.parent
    if isinstance(group, Tag) and group.name in ("thead", "tbody", "tfoot"):
        for sibling in group.find_next_siblings(["thead", "tbody", "tfoot"]):
            if sibling.find("tr") is not None:
                return False
    return True


class _Space(str):
    """A whitespace-only run from a collapsible text node."""


def _collect(node: Tag, out: list, ws: str) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            if isinstance(child, _NON_TEXT_STRINGS):
                continue
            text = str(child)
            if ws == "normal":
                text = _COLLAPSIBLE_RE.sub(" ", text)
                out.append(_Space(text) if text == " " else text)
            elif ws == "pre-line":
                out.append(re.sub(r"[ \t\r\f]+", " ", text))
            else:
                out.append(text)
            continue
        if not isinstance(child, Tag) or child.name in _SKIP_TAGS or is_hidden(child):
            continue
        if child.name == "br":
            out.append("\n")
            continue
        display = _display(child)
        breaks = 2 if display == "p" else 1 if display == "block" else 0
        if breaks:
            out.append(breaks)
        _collect(child, out, _white_space(child, ws))
        if breaks:
            out.append(breaks)
        elif display == "cell" and not _is_last_cell(child):
            out.append("\t")
        elif display == "row" and not _is_last_row(child):
            out.append("\n")


_EDGE = object()


def _join(items: list) -> str:
    # A collapsible space next to a line boundary renders nothing; drop it so it
    # cannot split a run of required line breaks into two lines. Neighbours are
    # the nearest items that are not themselves such spaces.
    n = len(items)
    prev: list = [_EDGE] * n
    nearest = _EDGE
    for i, item in enumerate(items):
        prev[i] = nearest
        if not isinstance(item, _Space):
            nearest = item
    kept: list = []
    following = _EDGE
    for i in range(n - 1, -1, -1):
        item = items[i]
        if isinstance(item, _Space):
            before = prev[i]
            if before is _EDGE or following is _EDGE or isinstance(before, int) or isinstance(following, int):
                continue
        else:
            following = item
        kept.append(item)
    kept.reverse()

    parts: list[str] = []
    pending = 0
    for item in kept:
        if isinstance(item, int):
            pending = max(pending, item)
            continue
        if not item:
            continue
        if pending and parts:
            parts.append("\n" * pending)
        pending = 0
        parts.append(item)
    return "".join(parts)


def normalize_text(text: str) -> str:
    """Tidy extracted text: plain spaces, trimmed lines, at most one blank line in a row."""
    text = text.replace(" ", " ").replace("\r", "")
    lines = [_SPACES_RE.sub(" ", line).strip(" \t") for line in text.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip("\n")


def element_text(el: Tag | None) -> str:
    """The visible text of *el* with its line and paragraph breaks kept."""
    if el is None:
        return ""
    items: list = []
    _collect(el, items, _white_space(el, "normal"))
    return normalize_text(_join(items))


def one_line(text: str) -> str:
    """Collapse any whitespace, line breaks included, to single spaces."""
    return " ".join(text.split())


# Not rendered at all, so nothing under them is visible (images and form
# controls are rendered, so iter_visible still yields those).
_NEVER_RENDERED = frozenset({"script", "style", "noscript", "template", "head", "title", "meta", "link", "base"})


def iter_visible(el: Tag) -> Iterator[Tag]:
    """Descendant tags of *el* in document order, skipping hidden subtrees."""
    for child in el.children:
        if not isinstance(child, Tag) or child.name in _NEVER_RENDERED or is_hidden(child):
            continue
        yield child
        yield from iter_visible(child)


def _is_emoji_glyph(href: str) -> bool:
    """ParentSquare's editor wraps emoji in a link to Google's noto-emoji PNG."""
    try:
        parts = urlsplit(href)
    except ValueError:
        return False
    return (parts.hostname or "").lower() == "fonts.gstatic.com" and "/notoemoji/" in parts.path


_TOGGLE_CLASSES = frozenset({"description-link", "hide-show-link", "read-more", "read-less"})


def element_links(el: Tag | None, base_url: str = BASE_URL) -> list[Link]:
    """Every link a reader can follow inside *el*, as ``Link(text, href)``.

    Skipped on purpose: in-page anchors (``#…``) and ``javascript:`` links, the
    "Read More"/"Read Less" toggles, and the emoji-glyph links ParentSquare's
    editor wraps around emoji. Relative hrefs are made absolute. Exact
    duplicates (same text and href) are listed once.
    """
    if el is None:
        return []
    links: list[Link] = []
    seen: set[tuple[str, str]] = set()
    for tag in iter_visible(el):
        if tag.name != "a" or not tag.get("href"):
            continue
        href = tag["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if set(tag.get("class") or ()) & _TOGGLE_CLASSES or tag.get("title") == "hide-show-link":
            continue
        if _is_emoji_glyph(href):
            continue
        href = urljoin(base_url + "/", href)
        text = one_line(element_text(tag)) or tag.get("aria-label") or tag.get("title") or href
        key = (text, href)
        if key in seen:
            continue
        seen.add(key)
        links.append(Link(text=text, href=href))
    return links
