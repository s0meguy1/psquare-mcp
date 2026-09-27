from __future__ import annotations

import re
from datetime import datetime, tzinfo

from bs4 import BeautifulSoup, Tag

from parentsquare_mcp.models import Attachment, Conversation, Message
from parentsquare_mcp.parsers.dates import parse_day_and_time, parse_numeric_datetime
from parentsquare_mcp.parsers.feeds import check_drift
from parentsquare_mcp.parsers.text import element_links, element_text, one_line
from parentsquare_mcp.urls import is_attachment_href

_THREAD_CLASS_RE = re.compile(r"a-chat-thread-(\d+)")
_THREAD_HREF_RE = re.compile(r"/chats/(\d+)")
_MESSAGE_ID_RE = re.compile(r"^chat-message-(\d+)$")
_USER_HREF_RE = re.compile(r"/users/(\d+)")


def _thread_links(soup: BeautifulSoup) -> list[Tag]:
    scope = soup.find(id="chat-threads-container") or soup
    links = scope.find_all("a", class_=re.compile(r"a-chat-thread"))
    if not links:
        links = scope.find_all(attrs={"data-testid": re.compile(r"^chat-thread-item-\d+$")})
    return links


def parse_conversation_list(soup: BeautifulSoup, tz: tzinfo | None = None) -> list[Conversation]:
    """Parse /schools/{id}/users/{uid}/chats -> list of Conversation.

    Structure (live, 2026-09):
      #chat-threads-container > a.a-chat-thread.a-chat-thread-{id}[href=.../chats/{id}]
        [aria-label="Conversation with X. 3 messages total. Last message sent: Thu 9/10/26, 8:29 am"]
        i[data-testid=chat-thread-type-icon].fa-user | .fa-users   (1:1 vs group)
        span.user-thread-chat-name-{id}    (participant names)
        span.badge                         (message count)
        div.date                           ("Thu 9/10/26, 8:29 am" — has a year)
        span.reaction-icons                (preview text)

    ``last_message_at`` is exact to the minute; pass the school's *tz* to make
    it aware. Raises ``ParseDriftError`` when the page lists threads but none
    parse.
    """
    convos: list[Conversation] = []
    links = _thread_links(soup)
    for link in links:
        href = link.get("href", "")
        chat_id = 0
        id_match = _THREAD_HREF_RE.search(href) or _THREAD_CLASS_RE.search(" ".join(link.get("class", [])))
        if not id_match:
            id_match = re.search(r"(\d+)$", link.get("data-testid", ""))
        if id_match:
            chat_id = int(id_match.group(1))
        if not chat_id:
            continue

        name_span = link.find("span", class_=re.compile(r"user-thread-chat-name"))
        participants: list[str] = []
        if name_span:
            participants = [n.strip() for n in one_line(element_text(name_span)).split(",") if n.strip()]

        date_div = link.find("div", class_="date")
        date = one_line(element_text(date_div)) if date_div else ""

        preview_el = link.find("span", class_="reaction-icons") or link.find("div", class_="chat-message")
        preview = one_line(element_text(preview_el)) if preview_el else ""

        aria = link.get("aria-label", "")
        badge = link.find("span", class_="badge")
        count_match = re.search(r"(\d+)", badge.get_text()) if badge else None
        if not count_match:
            count_match = re.search(r"(\d+)\s+messages?\s+total", aria, re.I)
        message_count = int(count_match.group(1)) if count_match else 0

        icon = link.find(attrs={"data-testid": "chat-thread-type-icon"}) or link.find("i", class_=re.compile(r"^fa-users?$"))
        icon_classes = set(icon.get("class") or ()) if icon else set()
        is_group = "fa-users" in icon_classes

        classes = " ".join(link.get("class", []))
        unread = "unread" in aria.lower() or "unread" in classes.lower() or link.find(class_=re.compile("unread")) is not None

        last_text = date or (re.search(r"Last message sent:\s*(.+)$", aria).group(1) if "Last message sent:" in aria else "")
        convos.append(
            Conversation(
                id=chat_id,
                participants=participants,
                last_message_preview=preview[:200],
                date=date,
                unread=unread,
                is_group=is_group,
                message_count=message_count,
                last_message_at=parse_numeric_datetime(last_text, tz),
            )
        )

    check_drift("chat list", len(links), len(convos), "conversations")
    return convos


def _message_elements(soup: BeautifulSoup | Tag) -> list[Tag]:
    els = soup.find_all("div", id=_MESSAGE_ID_RE)
    if not els:
        els = soup.find_all(attrs={"data-testid": re.compile(r"^chat-message-item-\d+$")})
    return els


def _message_attachments(el: Tag) -> list[Attachment]:
    attachments: list[Attachment] = []
    seen: set[str] = set()
    # Images: the viewer opens a.thumbnail[data-url] (full size); the <img> is a thumbnail.
    for a in el.find_all("a", class_="thumbnail"):
        img = a.find("img")
        full = a.get("data-url") or a.get("data-fallback-url") or (img.get("src", "") if img else "")
        if not full or full in seen:
            continue
        seen.add(full)
        alt = (img.get("alt") or None) if img else None
        attachments.append(Attachment(name=alt or "image", url=full, file_type="image",
                                      thumbnail_url=img.get("src") if img else None, alt=alt))
    image_names = {a.name for a in attachments}
    # Older layout: bare <img> tags hosted by ParentSquare
    if not attachments:
        for img in el.find_all("img"):
            src = img.get("src", "")
            if src and "parentsquare" in src and src not in seen:
                seen.add(src)
                attachments.append(Attachment(name=img.get("alt") or "image", url=src, file_type="image",
                                              alt=img.get("alt") or None))
    # Files: signed S3 downloads (an image's own download link is skipped)
    for a_tag in el.find_all("a", href=True):
        href = a_tag["href"]
        if not is_attachment_href(href) or href in seen:
            continue
        name = re.sub(r"^Download\s*:?\s*", "", one_line(element_text(a_tag))).strip() or "file"
        if name in image_names:
            continue
        seen.add(href)
        attachments.append(Attachment(name=name, url=href, file_type="document"))
    return attachments


def parse_chat_thread(soup: BeautifulSoup, tz: tzinfo | None = None, now: datetime | None = None,
                      chat_id: int | None = None) -> list[Message]:
    """Parse /schools/{id}/users/{uid}/chats/{chat_id} -> list of Message.

    Structure (live, 2026-09):
      #chat-messages[data-thread-id][data-current-user-id]
        .chat-day-header-text                    ("Thu, Sep 10" — no year)
        div#chat-message-{id}.chat-box.chat-thread(.pull-right = mine | .received-message.pull-left)
          .chat-message-meta span.user a         (author)
          div.chat-message.row > .col-xs-12      (text)
          .chat-attachments a.thumbnail[data-url] (image; data-url is the viewer's full size)
          .chat-message-timestamp span.date      ("8:13 am")

    Each message carries its DOM id, the thread id, whether this parent sent it,
    its text with line breaks and links, and ``posted_at``: the day heading's
    date (year inferred from its weekday, see ``parsers.dates``) at the shown
    time, in *tz* when given (the school's zone) and naive otherwise. The page
    renders the whole thread (checked against the list's message counts); no
    request loads older messages.

    Raises ``ParseDriftError`` when the page holds messages but none parse.
    """
    scroller = soup.find(id="chat-messages") or soup.find(attrs={"data-thread-id": True})
    if chat_id is None and scroller is not None and str(scroller.get("data-thread-id", "")).isdigit():
        chat_id = int(scroller["data-thread-id"])
    current_user = None
    if scroller is not None and str(scroller.get("data-current-user-id", "")).isdigit():
        current_user = int(scroller["data-current-user-id"])

    elements = _message_elements(soup)
    wanted = {id(e) for e in elements}
    messages: list[Message] = []
    current_day = ""
    for el in soup.find_all(True):
        classes = el.get("class") or ()
        if "chat-day-header-text" in classes:
            current_day = one_line(element_text(el))
            continue
        if id(el) not in wanted:
            continue

        user_span = el.find("span", class_="user")
        author_el = user_span.find("a") if user_span else None
        author = one_line(element_text(author_el)) if author_el else ""

        text_el = None
        row = el.find("div", class_="chat-message")
        if row is not None:
            text_el = row.find("div", class_="col-xs-12") or row
        text = element_text(text_el) if text_el is not None else ""

        ts_el = el.find("div", class_="chat-message-timestamp")
        time_str = ""
        if ts_el:
            date_span = ts_el.find("span", class_="date")
            time_str = one_line(element_text(date_span or ts_el))

        if not (author or text_el is not None or ts_el is not None):
            continue  # unrecognisable; counted by the drift check below

        if "pull-right" in classes:
            is_mine: bool | None = True
        elif "received-message" in classes or "pull-left" in classes:
            is_mine = False
        else:
            is_mine = None
        if is_mine is None and current_user and author_el is not None:
            m = _USER_HREF_RE.search(author_el.get("href", ""))
            if m:
                is_mine = int(m.group(1)) == current_user

        id_match = _MESSAGE_ID_RE.match(el.get("id", "")) or re.search(r"(\d+)$", el.get("data-testid", ""))
        full_date = f"{current_day} {time_str}".strip() if current_day else time_str
        messages.append(
            Message(
                author=author,
                date=full_date,
                text=text,
                attachments=_message_attachments(el),
                id=int(id_match.group(1)) if id_match else None,
                chat_id=chat_id,
                is_mine=is_mine,
                posted_at=parse_day_and_time(current_day, time_str, tz, now),
                links=element_links(text_el) if text_el is not None else [],
            )
        )

    check_drift("chat thread", len(elements), len(messages), "messages")
    return messages
