from __future__ import annotations

import re
from datetime import datetime, tzinfo

from bs4 import BeautifulSoup, Tag

from parentsquare_mcp.config import BASE_URL
from parentsquare_mcp.models import Notice
from parentsquare_mcp.parsers.dates import parse_display_datetime
from parentsquare_mcp.parsers.feeds import check_drift
from parentsquare_mcp.parsers.text import element_links, element_text, one_line

_BOX_ID_RE = re.compile(r"^notice-([a-z_]+)-(\d+)$")
_TAB_ID_RE = re.compile(r"-(\d+)$")


def _notice_boxes(soup: BeautifulSoup) -> list[Tag]:
    """Every notice on the page: ``div#notice-<type>-<id>`` or a ``ps-box.notice-type-*``."""
    boxes = []
    for box in soup.find_all("div"):
        box_id = box.get("id", "")
        classes = " ".join(box.get("class") or ())
        if _BOX_ID_RE.match(box_id) or ("ps-box" in classes and "notice-type-" in classes):
            boxes.append(box)
    return boxes


def parse_notices(soup: BeautifulSoup, school_id: int | None = None, tz: tzinfo | None = None,
                  now: datetime | None = None) -> list[Notice]:
    """Parse /schools/{id}/notices -> list of Notice (alerts and secure documents).

    Notice structure (live, 2026-09):
      div#notice-{type}-{id}.ps-box.notice-type-{alert|document}
        span.notice-title                           ("Alert: <subject>")
        .feed-metadata > span                       (date, "•", span.text-dark school/district)
        #smart-alert-email-message-{id}             (email: <strong> subject + body;
            .hide-on-click = truncated, .show-on-click = full)
        #smart-alert-text-message-{id}              (the SMS text)

    Alerts listed here do not appear in the school feed (checked live
    2026-09-27 against feed pages covering their dates), so this page is the
    only place a caller sees them. Each notice has a stable ``id``, a ``url``
    anchored on the notices page (there is no per-notice page), the full
    ``body``, the ``text_message``, the body's ``links`` and ``posted_at`` —
    the shown date with its year inferred from the weekday, in *tz* when given.
    The page only lists the past 3 weeks, so poll it at least that often.

    Raises ``ParseDriftError`` when the page holds notices but none parse.
    """
    notices: list[Notice] = []
    boxes = _notice_boxes(soup)
    for box in boxes:
        classes = " ".join(box.get("class", []))
        id_match = _BOX_ID_RE.match(box.get("id", ""))

        if "notice-type-alert" in classes:
            notice_type = "alert"
        elif "notice-type-document" in classes:
            notice_type = "document"
        elif id_match:
            notice_type = id_match.group(1)
        else:
            continue

        notice_id: int | None = int(id_match.group(2)) if id_match else None
        if notice_id is None:
            tab = box.find(id=re.compile(r"^smart-alert-(?:email|text)-(?:tab|message)-\d+$"))
            if tab is not None:
                notice_id = int(_TAB_ID_RE.search(tab["id"]).group(1))

        title_el = box.find("span", class_="notice-title")
        title = one_line(element_text(title_el)) if title_el else ""
        title = re.sub(r"^(Alert|Document)\s*:\s*", "", title)

        date = ""
        school = ""
        metadata = box.find("div", class_="feed-metadata")
        if metadata:
            for span in metadata.find_all("span", recursive=False):
                text = one_line(element_text(span))
                if not text or text == "•":
                    continue
                if "text-dark" in " ".join(span.get("class", [])):
                    school = text
                elif not date:
                    date = text

        email = box.find(id=re.compile(r"^smart-alert-email-message-\d+$"))
        text_tab = box.find(id=re.compile(r"^smart-alert-text-message-\d+$"))
        subject = ""
        body = ""
        if email is not None:
            strong = email.find("strong")
            subject = one_line(element_text(strong)) if strong else ""
            full = email.find(class_="show-on-click")
            body_el = full if full is not None else email
            body = element_text(body_el)
            body = re.sub(r"\s*Read Less\s*$", "", body)
            links = element_links(body_el)
        else:
            links = []
        text_message = element_text(text_tab) if text_tab is not None else ""
        if not body:
            body = text_message
            links = element_links(text_tab) if text_tab is not None else links
        if not title:
            title = subject

        if not (title or body or date):
            continue

        url = f"{BASE_URL}/schools/{school_id}/notices" if school_id else ""
        if url and box.get("id"):
            url += f"#{box['id']}"

        notices.append(Notice(
            title=title,
            notice_type=notice_type,
            date=date,
            school=school,
            id=notice_id,
            url=url,
            subject=subject,
            body=body,
            text_message=text_message,
            links=links,
            posted_at=parse_display_datetime(date, tz, now),
        ))

    check_drift("notices page", len(boxes), len(notices), "notices")
    return notices
