from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from parentsquare_mcp.config import BASE_URL
from parentsquare_mcp.errors import ParseDriftError
from parentsquare_mcp.models import (
    Attachment,
    Comment,
    EventInfo,
    FeedPost,
    FormInfo,
    PostDetail,
    SignupItem,
)
from parentsquare_mcp.parsers.dates import parse_google_calendar_dates, parse_timestamp
from parentsquare_mcp.parsers.text import element_links, element_text, is_hidden, one_line
from parentsquare_mcp.urls import is_attachment_href

logger = logging.getLogger(__name__)

# Random hash prefix pattern: 16+ alphanumeric chars followed by underscore
_HASH_PREFIX_RE = re.compile(r"^[a-zA-Z0-9]{16,}_")
# Inline image uploads: "inline_images_<hex>_<epoch ms>-<original name>" (older
# uploads have no hex segment).
_INLINE_PREFIX_RE = re.compile(r"^inline_images_(?:[0-9a-f]+_)?\d+-?")
_FEED_ID_RE = re.compile(r"^feed_(\d+)$")
_ATTACHMENT_PATH_RE = re.compile(r"^(?:https?://(?:www\.)?parentsquare\.com)?/feeds/\d+/attachment/\d+")
_COMMENT_ID_RE = re.compile(r"^comment[-_](\d+)$")

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff")
_VIDEO_EXT = (".mp4", ".mov", ".m4v", ".avi", ".webm", ".wmv")


def _filename_from_url(url: str) -> str:
    """Extract a human-readable filename from a URL, URL-decoding it."""
    try:
        parsed = urlparse(url)
        path = parsed.path
        name = unquote(PurePosixPath(path).name.replace("+", " "))
        # Strip the upload prefix, e.g. "inline_images_e2dd…_1789753655987-"
        name = _INLINE_PREFIX_RE.sub("", name)
        # Strip "thumb_" prefix from CDN thumbnail URLs
        name = re.sub(r"^thumb_", "", name)
        # Strip random hash prefixes like "fUMwIIoARyb7Gs2reHSA_"
        name = _HASH_PREFIX_RE.sub("", name)
        # If only an extension remains (e.g. ".png"), treat as generic image
        if not name.strip() or re.match(r"^\.\w+$", name.strip()):
            return "image"
        return name.strip()
    except Exception:
        return "file"


def _disposition_filename(url: str) -> str | None:
    """Extract the original upload filename from a response-content-disposition query param."""
    try:
        qs = parse_qs(urlparse(url).query)
        disp = unquote(qs.get("response-content-disposition", [""])[0])
        m = re.search(r'filename="?([^"]+)"?', disp)
        return m.group(1).strip() if m else None
    except Exception:
        return None


def _url_path_key(url: str) -> str:
    """Normalize a URL to just its path for cross-referencing thumbnail vs download URLs."""
    return unquote(urlparse(url).path)


def _file_type(name: str) -> str:
    lower = name.lower()
    if lower.endswith(_IMAGE_EXT):
        return "image"
    if lower.endswith(_VIDEO_EXT):
        return "video"
    return "document"


def _abs(href: str) -> str:
    return urljoin(BASE_URL + "/", href)


# ---------------------------------------------------------------------------
# Finding posts
# ---------------------------------------------------------------------------


def _feed_id_elements(soup: BeautifulSoup | Tag) -> list[Tag]:
    return soup.find_all("div", id=_FEED_ID_RE)


def iter_post_boxes(soup: BeautifulSoup | Tag) -> Iterator[tuple[int, Tag]]:
    """Yield ``(feed_id, box)`` for every post on a feed-shaped page.

    Posts are found by their ``div#feed_<id>`` element at any depth, so both
    layouts parse: the old ``#feeds-list > div.ps-box`` and the live
    ``#feeds-list > ul.feeds-list > li.feeds-list-item > div.ps-box`` (W1).
    Each id is yielded once. The box is the post's ``div.ps-box`` when that box
    holds this post alone, else the ``div#feed_<id>`` element itself, which
    holds the title, metadata, body and actions in both layouts.
    """
    scope = soup.find(id="feeds-list") or soup
    seen: set[int] = set()
    for el in _feed_id_elements(scope):
        feed_id = int(_FEED_ID_RE.match(el["id"]).group(1))
        if feed_id in seen:
            continue
        seen.add(feed_id)
        box = el
        ps_box = el.find_parent("div", class_="ps-box")
        if ps_box is not None:
            ids = {m.group(1) for d in _feed_id_elements(ps_box) if (m := _FEED_ID_RE.match(d["id"]))}
            if len(ids) == 1:
                box = ps_box
        yield feed_id, box


def count_feed_markers(soup: BeautifulSoup | Tag) -> int:
    """How many distinct posts the page evidently holds (``div#feed_<id>`` anywhere)."""
    return len({m.group(1) for d in _feed_id_elements(soup) if (m := _FEED_ID_RE.match(d["id"]))})


def check_drift(page: str, markers: int, parsed: int, what: str = "items") -> None:
    """Raise when a page evidently holds items but none parsed; warn when some are lost."""
    if markers and not parsed:
        raise ParseDriftError(
            page, f"the page holds {markers} {what} but none could be parsed — the markup has probably changed",
            markers=markers,
        )
    if parsed < markers:
        logger.warning("%s: parsed %d of %d %s; the rest were skipped", page, parsed, markers, what)


# ---------------------------------------------------------------------------
# Pieces of a post
# ---------------------------------------------------------------------------


def _title(box: Tag) -> str:
    heading = box.find(attrs={"role": "heading"})
    title = one_line(element_text(heading)) if heading else ""
    if not title:
        subject = box.find("div", class_="subject")
        if subject:
            link = subject.find("a", recursive=False)
            title = one_line(element_text(link)) if link else ""
            if not title:
                # Poll detail pages put the question in a bare text node
                for child in subject.children:
                    if isinstance(child, str) and child.strip():
                        title = child.strip()
                        break
    if not title:
        region = box if box.get("aria-label") else box.find(attrs={"role": "region", "aria-label": True})
        if region is not None:
            title = one_line(region.get("aria-label", ""))
    return title


def _author(box: Tag) -> str:
    author_el = box.find("a", class_="user-name")
    if author_el:
        return one_line(element_text(author_el))
    # District and department posts name the sender without a profile link
    sender = box.find(class_="feed-metadata-from")
    return one_line(element_text(sender)) if sender else ""


def _timestamp(box: Tag) -> str:
    time_el = box.find("span", class_="time-ago")
    if not time_el:
        return ""
    return time_el.get("data-timestamp", "") or one_line(element_text(time_el))


def _feed_description(box: Tag) -> Tag | None:
    """The full body on a feed page: the (CSS-hidden) expanded copy, else the truncated one."""
    for expanded in box.find_all("div", class_="expanded-text"):
        desc = expanded.find("div", class_="description")
        if desc is not None:
            return desc
    return box.find("div", class_="description")


def _hidden_within(el: Tag, stop: Tag) -> bool:
    """Is *el*, or an ancestor below *stop*, hidden?"""
    node = el
    while node is not None and node is not stop:
        if isinstance(node, Tag) and is_hidden(node):
            return True
        node = node.parent
    return False


def _detail_description(feed_show: Tag | None) -> Tag | None:
    """The visible body on a post page: the longest ``.description`` that is not hidden."""
    if feed_show is None:
        return None
    best, best_len = None, -1
    for desc in feed_show.find_all("div", class_="description"):
        if _hidden_within(desc, feed_show):
            continue
        text_len = len(desc.get_text())
        if text_len > best_len:
            best, best_len = desc, text_len
    return best


_READ_MORE_RE = re.compile(r"\s*Read More\s*$")


def _classes_in(box: Tag) -> set[str]:
    return {c for el in box.find_all(True) for c in (el.get("class") or ())}


def _pills(box: Tag) -> list[str]:
    nav = box.find("ul", class_="nav-pills")
    return [one_line(element_text(li)) for li in nav.find_all("li")] if nav else []


def _feed_icon_classes(box: Tag) -> set[str]:
    icon = box.find("div", class_="feed-icon")
    return {c for i in (icon.find_all("i") if icon else []) for c in (i.get("class") or ())}


_PINNED_CLASS_RE = re.compile(r"(^|-)pinned($|-)|^fa-thumb-?tack$")
_URGENT_CLASS_RE = re.compile(r"urgent|smart-alert")


def _is_pinned(box: Tag) -> bool:
    """A pinned post: a thumb-tack icon, a *pinned* class, or a "Pinned" badge.

    The badge must be an element of its own whose whole text is "Pinned", so a
    post merely titled "Pinned: …" does not count.
    """
    if any(_PINNED_CLASS_RE.search(c) for c in _classes_in(box) | set(box.get("class") or ())):
        return True
    title_area = box.find(class_="feed-title")
    if title_area is None:
        return False
    return any(
        one_line(el.get_text()).lower() == "pinned" and el.find_parent(attrs={"role": "heading"}) is None
        and el.get("role") != "heading"
        for el in title_area.find_all(["span", "div", "i", "small", "label"])
    )


def _is_urgent(box: Tag) -> bool:
    """An urgent/alert post: an *urgent*/*smart-alert* class or a bullhorn feed icon."""
    if any(_URGENT_CLASS_RE.search(c) for c in _classes_in(box) | set(box.get("class") or ())):
        return True
    return "fa-bullhorn" in _feed_icon_classes(box)


def _kinds(box: Tag) -> list[str]:
    """Every kind of post this box is, in a fixed order."""
    classes = _classes_in(box)
    pills = " ".join(_pills(box))
    kinds: list[str] = []
    if "feed-cal" in classes or "fa-calendar" in _feed_icon_classes(box) or re.search(r"\bRSVP\b", pills):
        kinds.append("event")
    has_price = "wish-list-item-price" in classes or "payment-list-head" in classes
    if ("volunteer-list" in classes and not has_price) or re.search(r"\bSign Up\b", pills):
        kinds.append("signup")
    if "signable-form" in classes or re.search(r"\b(Complete|Sign|View) Form\b", pills):
        kinds.append("form")
    if "poll-option" in classes:
        kinds.append("poll")
    if has_price or re.search(r"\bPay\b", pills):
        kinds.append("payment")
    if "feed-image-thumbnail" in classes or box.select_one("ul.attachments li.image a.thumbnail"):
        kinds.append("photo")
    return kinds


def _post_type(kinds: list[str], pinned: bool, box: Tag) -> str:
    """One headline kind, keeping 0.4.0's values ("pinned", "event", "photo")."""
    if pinned:
        return "pinned"
    icon = _feed_icon_classes(box)
    if "fa-camera" in icon:
        return "photo"
    for kind in ("event", "signup", "form", "poll", "payment", "photo"):
        if kind in kinds:
            return kind
    return ""


def _event(box: Tag) -> EventInfo | None:
    cal = box.find("div", class_="feed-cal")
    if cal is None:
        return None
    event = EventInfo()
    button = cal.find("button")
    label = button.get("aria-label", "") if button else ""
    label = re.sub(r"\s*-\s*Add (?:RSVP date )?to calendar\s*$", "", label, flags=re.I)
    label = re.sub(r"(?<=[AP]M)To\b", " To ", label)
    event.label = one_line(label)
    google = cal.find("a", href=re.compile(r"google\.com/calendar"))
    if google:
        qs = parse_qs(urlparse(google["href"]).query)
        event.start, event.end = parse_google_calendar_dates(qs.get("dates", [""])[0])
        event.title = qs.get("text", [""])[0]
        event.location = qs.get("location", [None])[0] or None
    ics = cal.find("a", href=re.compile(r"\.ics(\?|$)"))
    if ics:
        event.ics_url = _abs(ics["href"])
    rsvp = box.find(class_=re.compile(r"rsvp", re.I))
    if rsvp is not None:
        chosen = rsvp.find(class_=re.compile(r"\b(active|selected|chosen|checked)\b"))
        if chosen is not None:
            answer = one_line(element_text(chosen)).lower()
            event.rsvp = next((a for a in ("yes", "no", "maybe") if answer.startswith(a)), answer or None)
    return event


_SIGNED_RE = re.compile(
    r"\b(you (have )?(signed|submitted|completed)|signed on|submitted on|already (signed|submitted))\b", re.I)


def _form(box: Tag) -> FormInfo | None:
    form_el = box.find("div", class_="signable-form")
    pills = " ".join(_pills(box))
    if form_el is None and not re.search(r"\b(Complete|Sign|View) Form\b", pills):
        return None
    info = FormInfo()
    if form_el is not None:
        head = form_el.find(class_="wish-list-head")
        info.title = one_line(element_text(head)) if head else ""
        due = form_el.find(class_="wish-list-date")
        info.due = one_line(element_text(due)) if due else ""
        note = form_el.find(class_="wish-list-repeat")
        info.note = one_line(element_text(note)) if note else ""
        info.closed = form_el.find("fieldset", disabled=True) is not None
        info.questions = [
            one_line(element_text(label)).rstrip(" *")
            for label in form_el.find_all("label", class_="control-label")
            if one_line(element_text(label))
        ]
        text = element_text(form_el)
    else:
        text = ""
    if _SIGNED_RE.search(text) or _SIGNED_RE.search(pills) or box.find(class_=re.compile(r"form-signed|signed-form")):
        info.signed = True
    elif re.search(r"\b(Complete|Sign) Form\b", pills):
        info.signed = False
    elif form_el is not None:
        # The server renders a submitted form with its saved answers, so answer
        # fields that exist but are all empty mean this parent has not signed.
        answers = form_el.find_all(["input", "textarea", "select"], attrs={"name": re.compile(r"form_answers_attributes")})
        answers = [a for a in answers if a.get("type") != "hidden"]
        if answers:
            filled = any(
                (a.name == "input" and a.get("type") in ("radio", "checkbox") and a.has_attr("checked"))
                or (a.name == "input" and a.get("type") not in ("radio", "checkbox") and a.get("value"))
                or (a.name == "textarea" and a.get_text(strip=True))
                or (a.name == "select" and a.find("option", selected=True) is not None)
                for a in answers
            )
            info.signed = filled
    return info


def _signup_progress(box: Tag) -> str:
    metadata_el = box.find(class_="feed-metadata")
    if not metadata_el:
        return ""
    highlights = metadata_el.find_all("div", class_="color-highlight", recursive=False)
    parts = [one_line(element_text(h)) for h in highlights]
    return " • ".join(p for p in parts if re.match(r"\d+/\d+", p))


def _attachment_label_name(a: Tag) -> str:
    label = a.get("aria-label") or ""
    name = re.sub(r"^Download\s*:?\s*", "", label).strip()
    if not name:
        name = re.sub(r"^Download\s*:?\s*", "", one_line(element_text(a))).strip()
    return name


def _attachments(scope: Tag, description: Tag | None) -> list[Attachment]:
    """Every attachment a reader sees on a post: files, gallery images, inline images.

    * ``ul.attachments`` links to ``/feeds/{id}/attachment/{aid}``, the live file
      layout; the name is the link's "Download <name>" label. The link answers
      with a redirect to a signed S3 URL, so fetching it needs no special case.
    * Gallery images: ``a.thumbnail[data-url]`` (full size in ``data-url``) or
      the older ``img.feed-image-thumbnail`` (full size in ``fallback``).
    * Images inside the body. Their ``src`` is already the full-size upload
      (verified against the browser: natural size equals the file's).
    * Direct S3/CloudFront links anywhere on the post (older layout).
    """
    download_names: dict[str, str] = {}
    download_urls: dict[str, str] = {}
    for link in scope.find_all("a", href=True):
        href = link["href"]
        if is_attachment_href(href):
            path_key = _url_path_key(href)
            disp_name = _disposition_filename(href)
            if disp_name:
                download_names[path_key] = disp_name
            download_urls[path_key] = href

    attachments: list[Attachment] = []
    seen: set[str] = set()

    def add(att: Attachment, key: str) -> None:
        if key in seen:
            return
        seen.add(key)
        attachments.append(att)

    # Gallery images, new layout
    for a in scope.select("ul.attachments li.image a.thumbnail"):
        img = a.find("img")
        full = a.get("data-url") or a.get("data-fallback-url") or (img.get("src", "") if img else "")
        if not full:
            continue
        alt = (img.get("alt") or None) if img else None
        name = download_names.get(_url_path_key(full)) or alt or _filename_from_url(full)
        add(Attachment(name=name, url=full, file_type="image", thumbnail_url=img.get("src") if img else None,
                       alt=alt), _url_path_key(full))

    # Gallery images, older layout
    for img in scope.find_all("img", class_="feed-image-thumbnail"):
        thumb_url = img.get("src", "")
        full_url = img.get("fallback", "") or thumb_url
        path_key = _url_path_key(full_url)
        name = download_names.get(path_key) or _filename_from_url(full_url)
        best_url = download_urls.get(path_key, full_url)
        add(Attachment(name=name, url=best_url, file_type="image", thumbnail_url=thumb_url,
                       alt=img.get("alt") or None), path_key)

    # Inline images in the body
    if description is not None:
        for img in description.find_all("img"):
            src = img.get("src", "")
            if not src or "avatar" in src or "logo" in src:
                continue
            path_key = _url_path_key(src)
            name = download_names.get(path_key) or _filename_from_url(src)
            add(Attachment(name=name, url=src, file_type="image", alt=img.get("alt") or None), path_key)

    # Files: /feeds/{id}/attachment/{aid} links (live layout)
    for a in scope.find_all("a", href=_ATTACHMENT_PATH_RE):
        if a.find_parent("li", class_="image") is not None:
            continue  # a gallery image's own download link
        url = _abs(a["href"])
        name = _attachment_label_name(a) or "file"
        add(Attachment(name=name, url=url, file_type=_file_type(name)), _url_path_key(url))

    # Files: direct S3/CloudFront links (older layout)
    for link in scope.find_all("a", href=True):
        href = link["href"]
        if not is_attachment_href(href):
            continue
        path_key = _url_path_key(href)
        name = _disposition_filename(href)
        if not name:
            name = re.sub(r"^Download\s*:?\s*", "", one_line(element_text(link))).strip() or _filename_from_url(href)
        add(Attachment(name=name, url=href, file_type=_file_type(name)), path_key)

    return attachments


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

_COMMENT_CHROME = re.compile(
    r"comment-full-name|user-name|time-ago|comment-time|comment-date|delete-comment|reply-link|"
    r"comment-actions|comment-reply|replies|reply-form|comment-content-translated|"
    r"translate|avatar|initials|appreciat")
_NOT_COMMENTS = re.compile(r"comments-form|comments-head|comment-count|comment-hint|new_comment")
_ACTION_WORDS = re.compile(
    r"^(reply|delete|edit|remove|appreciate|like|translate|see original|show original|report)$", re.I)


def _comment_id(el: Tag) -> int | None:
    for value in (el.get("id", ""), el.get("data-comment-id", ""), el.get("data-id", "")):
        m = _COMMENT_ID_RE.match(value) or re.fullmatch(r"(\d+)", value or "")
        if m:
            return int(m.group(1))
    return None


def _comment_text(el: Tag) -> Tag:
    """The element holding the comment's own words, stripped of name, date and buttons."""
    for cls in ("comment-content-original", "comment-body", "comment-text", "comment-message"):
        found = el.find(class_=cls)
        if found is not None and found.name != "textarea":
            return found
    copy = BeautifulSoup(str(el), "html.parser")
    root = copy.find(True)
    for tag in list(root.find_all(True)):
        if tag.decomposed:
            continue
        classes = " ".join(tag.get("class") or ())
        if (classes and _COMMENT_CHROME.search(classes)) or tag.name in ("form", "button", "time", "textarea"):
            tag.decompose()
        elif tag.name == "a" and (_ACTION_WORDS.match(one_line(tag.get_text())) or "/users/" in tag.get("href", "")):
            tag.decompose()
    return root


def _parse_comment(el: Tag, reply_class: str | None) -> Comment | None:
    author_el = (el.find("a", class_="comment-full-name") or el.find("a", class_="user-name")
                 or el.find(class_="comment-author") or el.find("a", href=re.compile(r"/users/\d+")))
    author = one_line(element_text(author_el)) if author_el else ""
    time_el = el.find(attrs={"data-timestamp": True}) or el.find(class_=re.compile(r"time-ago|comment-time|comment-date"))
    stamp = time_el.get("data-timestamp", "") if time_el is not None else ""
    date = stamp or (one_line(element_text(time_el)) if time_el is not None else "")
    if not date:
        time_tag = el.find("time")
        date = (time_tag.get("datetime") or one_line(element_text(time_tag))) if time_tag else ""
        stamp = stamp or (time_tag.get("datetime", "") if time_tag else "")
    body = _comment_text(el)
    text = element_text(body)
    if author and text.startswith(author):
        text = text[len(author):].lstrip(" :\n")
    replies: list[Comment] = []
    if reply_class:
        for r in el.find_all(class_=reply_class):
            reply = _parse_comment(r, None)
            if reply:
                replies.append(reply)
    if not text and not replies:
        return None
    return Comment(author=author, date=date, text=text, posted_at=parse_timestamp(stamp),
                   id=_comment_id(el), links=element_links(body), replies=replies)


def parse_comments(scope: Tag) -> list[Comment]:
    """Comments inside a post's ``.comments-box`` (W5), each with its replies.

    The markup follows the site's own selectors (``.comment``,
    ``.comment-container``, ``a.comment-full-name``, ``.comment-content-original``,
    ``.comment-reply``). Comments at a school with private comments only show
    the viewer's own thread, so an empty list is common and correct there.
    """
    box = scope.find("div", class_="comments-box") if "comments-box" not in (scope.get("class") or ()) else scope
    if box is None:
        return []
    container = box.find("div", class_="comments") or box
    items = [el for el in container.find_all(class_="comment")
             if el.find_parent(class_="comment") is None and el.name != "textarea"]
    if not items:
        # Older/unknown layout: each direct child with an author link or a timestamp
        items = [el for el in container.find_all("div", recursive=False)
                 if not _NOT_COMMENTS.search(" ".join(el.get("class") or ()) + " " + el.get("id", ""))
                 and (el.find("a", href=True) or el.find(attrs={"data-timestamp": True}))]
    comments: list[Comment] = []
    for el in items:
        comment = _parse_comment(el, "comment-reply")
        if comment:
            comments.append(comment)
    return comments


def _comment_count(scope: Tag) -> int | None:
    count_el = scope.find(class_="comment-count")
    if count_el is None:
        return None
    m = re.search(r"(\d+)\s+comments?", element_text(count_el), re.I)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Sign-ups
# ---------------------------------------------------------------------------

_MINE_RE = re.compile(r"cancel|remove|withdraw|unsign|signed-up|my-sign-?up", re.I)


def parse_signup_items(scope: Tag) -> list[SignupItem]:
    """Sign-up rows (volunteer slots, wish-list items, appointments) with their day."""
    items: list[SignupItem] = []
    for vol_list in scope.find_all("div", class_="volunteer-list"):
        if vol_list.find_parent("div", class_="volunteer-list") is not None:
            continue
        if vol_list.find(class_="wish-list-item-price") is not None:
            continue  # a payment list, parsed by parsers/payments.py
        current_date = ""
        for el in vol_list.find_all("div"):
            classes = el.get("class") or ()
            if "wish-list-date" in classes:
                current_date = one_line(element_text(el))
                continue
            if "wish-list-item-row" not in classes or {"payment-list-head", "payment-list-total"} & set(classes):
                continue
            name_el = el.find("div", class_="wish-list-item-name")
            item_name = one_line(element_text(name_el)) if name_el else ""
            if not item_name or item_name == "Item Name":
                continue
            time_el = el.find("div", class_="wish-list-item-time")
            time_slot = one_line(element_text(time_el)) if time_el else ""

            filled = 0
            total = 0
            filled_el = el.find("span", class_="count-filled")
            if filled_el:
                try:
                    filled = int(one_line(filled_el.get_text()))
                except ValueError:
                    pass
            qty_el = el.find("div", class_="wish-list-item-quantity")
            if qty_el:
                qty_match = re.search(r"(\d+)\s+of\s+(\d+)", qty_el.get_text())
                if qty_match:
                    filled = int(qty_match.group(1))
                    total = int(qty_match.group(2))
            if not total:
                open_el = el.find("div", class_="count-open")
                if open_el:
                    open_match = re.search(r"(\d+)\s+open", open_el.get_text())
                    if open_match:
                        total = filled + int(open_match.group(1))

            signed_up: list[str] = []
            signups_el = el.find("div", class_="wish-list-item-sign-ups")
            if signups_el:
                for entry in signups_el.find_all("li", class_="col-sign-ups-entry"):
                    person = one_line(element_text(entry)).lstrip("•").strip()
                    if person:
                        signed_up.append(person)

            action = el.find(class_="sign-up-col") or el
            action_classes = " ".join(c for t in action.find_all(True) for c in (t.get("class") or ()))
            action_text = one_line(element_text(action))
            mine = bool(_MINE_RE.search(action_classes) or re.search(r"\b(Cancel|Remove|Withdraw)\b", action_text))
            if mine:
                status = "mine"
            elif "closed-volunteer-sign-up" in action_classes or re.search(r"\b(Closed|Full)\b", action_text):
                status = "closed"
            elif "volunteer-sign-up" in action_classes or re.search(r"\bSign Up\b", action_text):
                status = "open"
            else:
                status = ""

            items.append(SignupItem(name=item_name, time_slot=time_slot, filled=filled, total=total,
                                    signed_up=signed_up, date=current_date, status=status, mine=mine))
    return items


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _parse_feed_box(feed_id: int, box: Tag) -> FeedPost | None:
    title = _title(box)
    author = _author(box)
    date = _timestamp(box)
    desc_el = _feed_description(box)
    summary = _READ_MORE_RE.sub("", element_text(desc_el))
    # Every real post has a byline or a date; a title alone (which can come from
    # the container's aria-label) means the post's own markup was not understood.
    if not (summary or (title and (author or date))):
        return None

    attachments = _attachments(box, desc_el)
    attachment_names = [a.name for a in attachments if a.name not in ("file", "image")]
    pills = " ".join(_pills(box))
    has_attachments = bool(attachments) or "Download" in pills

    comments = parse_comments(box)
    comment_count = len(comments) + sum(len(c.replies) for c in comments)
    if not comment_count:
        comments_box = box.find("div", class_="comments-box")
        if comments_box:
            count_match = re.search(r"(\d+)\s+comment", comments_box.get_text(), re.IGNORECASE)
            if count_match:
                comment_count = int(count_match.group(1))

    pinned = _is_pinned(box)
    kinds = _kinds(box)
    return FeedPost(
        id=feed_id,
        title=title,
        author=author,
        date=date,
        summary=summary,
        comment_count=comment_count,
        has_attachments=has_attachments,
        post_type=_post_type(kinds, pinned, box),
        attachment_names=attachment_names,
        signup_progress=_signup_progress(box),
        posted_at=parse_timestamp(date),
        links=element_links(desc_el),
        is_pinned=pinned,
        kinds=kinds,
        event=_event(box),
        form=_form(box),
        is_urgent=_is_urgent(box),
    )


def parse_feed_page(soup: BeautifulSoup, page: str = "feed page") -> list[FeedPost]:
    """Parse any feed-shaped page -> list of FeedPost.

    Used for the school feed (``/schools/{id}/feeds?page=N``), group feeds,
    sign-ups and forms. Posts are found at any depth (see ``iter_post_boxes``);
    each carries its title, author, raw ``date`` and parsed ``posted_at``, the
    full body text with its line breaks, the links in it, attachments, kind
    flags and event/form details.

    Raises ``ParseDriftError`` when the page holds posts (``div#feed_<id>``)
    but none of them parse, so a markup change cannot pass for an empty feed.
    *page* names the page in that error.

    A post whose own markup is not understood, on a page where others are, is
    still returned, with its id and whatever title it has, and logged: callers
    that diff post ids (to fetch each new post in full) must never lose one.
    """
    posts: list[FeedPost] = []
    unread: list[int] = []
    for feed_id, box in iter_post_boxes(soup):
        post = _parse_feed_box(feed_id, box)
        if post is None:
            unread.append(feed_id)
            post = FeedPost(id=feed_id, title=_title(box), author="", date="", summary="")
        posts.append(post)
    markers = count_feed_markers(soup)
    readable = len(posts) - len(unread)
    if markers and not readable:
        check_drift(page, markers, 0, "posts")  # raises ParseDriftError
    if unread:
        logger.warning(
            "%s: could not read %d of %d posts (%s); they are returned with their id and title only",
            page, len(unread), len(posts), ", ".join(map(str, unread)),
        )
    if len(posts) < markers:
        logger.warning("%s: %d post(s) sit outside the feed list and were not parsed", page, markers - len(posts))
    if posts and not any(p.date for p in posts):
        logger.warning("%s: no post has a timestamp; the date markup may have changed", page)
    return posts


def parse_post_detail(soup: BeautifulSoup) -> PostDetail:
    """Parse /feeds/{id} -> PostDetail with body, links, comments, attachments and kind details.

    Detail page structure (live, 2026-09):
      div#feed_{id} > .feed-show.feed-box
        .feed-title [role=heading]                 (title)
        .feed-metadata a.user-name | .feed-metadata-from   (author)
        .feed-metadata span.time-ago[data-timestamp]       (date)
        .description                                (body)
        ul.attachments a[href=/feeds/{id}/attachment/{aid}]  (files)
        .feed-cal / .signable-form / .volunteer-list        (event, form, sign-ups)
        .comments-box .comment-count + .comments            (comments)

    Raises ``ParseDriftError`` when the page is a post (it has a
    ``div#feed_<id>``) but neither a title nor a body nor an author parses.
    """
    feed_show = soup.find("div", class_="feed-show")

    feed_id_el = soup.find("div", id=_FEED_ID_RE)
    feed_id = int(_FEED_ID_RE.match(feed_id_el["id"]).group(1)) if feed_id_el else 0
    scope = feed_show or feed_id_el or soup

    title = _title(scope)
    author = _author(scope) or _author(soup)
    date = _timestamp(scope) or _timestamp(soup)

    desc_el = _detail_description(feed_show)
    body_text = _READ_MORE_RE.sub("", element_text(desc_el))
    if feed_id and not (title or body_text or author):
        raise ParseDriftError("post page", f"post {feed_id} has no parseable title, body or author", markers=1)

    comments = parse_comments(soup)
    comment_count = _comment_count(soup)
    parsed_comments = len(comments) + sum(len(c.replies) for c in comments)
    if comment_count and parsed_comments < comment_count:
        # Only a warning: at a school with private comments the count may include
        # comments this parent cannot see, so a shortfall is not proof of drift.
        logger.warning("post %s: page says %d comments, parsed %d", feed_id, comment_count, parsed_comments)

    pinned = _is_pinned(scope)
    return PostDetail(
        id=feed_id,
        title=title,
        author=author,
        date=date,
        body_text=body_text,
        comments=comments,
        attachments=_attachments(scope, desc_el),
        signup_items=parse_signup_items(soup),
        posted_at=parse_timestamp(date),
        links=element_links(desc_el),
        is_pinned=pinned,
        kinds=_kinds(scope),
        event=_event(scope),
        form=_form(scope),
        is_urgent=_is_urgent(scope),
        comment_count=comment_count,
    )
