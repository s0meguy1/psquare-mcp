"""Parsers against pages captured from the live site on 2026-09-27.

The fixtures in tests/fixtures/live_2026_09 are real server responses, scrubbed
by tests/fixtures/scrub.py: every word that is not ParentSquare's own UI text is
a same-length pseudo-word and every id is remapped, but the markup is exactly
what the site served. ``expected_browser.json`` holds what Chrome itself
reports for the same scrubbed files (loaded with the site's stylesheets):
``innerText`` of each body, summary, message and notice, and its anchors. The
text and link tests therefore compare the parsers with a real browser, not with
our own idea of what the page says. MANIFEST.json describes each page.

Every one of these parsers returned nothing, or run-together text, against the
live site before this suite existed (see the wish list's W1, W3, W4, W17).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup

from parentsquare_mcp.parsers.feeds import parse_feed_page, parse_post_detail
from parentsquare_mcp.parsers.groups import parse_group_feed
from parentsquare_mcp.parsers.messages import parse_chat_thread, parse_conversation_list
from parentsquare_mcp.parsers.notices import parse_notices
from parentsquare_mcp.parsers.polls import parse_polls_page
from parentsquare_mcp.parsers.students import parse_student_dashboard

FIXTURES = Path(__file__).parent / "fixtures" / "live_2026_09"
EXPECTED = json.loads((FIXTURES / "expected_browser.json").read_text(encoding="utf-8"))
IDS = json.loads((FIXTURES / "ids.json").read_text(encoding="utf-8"))  # the fixtures' (remapped) ids
EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)

PDF_POST = "post_pdf"                     # PDF attachment, emoji-glyph links, 2 inline images
EVENT_FORM_POST = "post_event_form"       # event (RSVP date) + signable form past its deadline
SIGNUP_POST = "post_signup"               # appointment sign-up, 21 slots over 2 days
NEWSLETTER_POST = "post_newsletter"       # paragraphs, bullets, 11 links, images with alt text
FLYER_POST = "post_flyer"                 # image-only flyer
CLASS_POST = "post_class_newsletter"      # class newsletter
POSTS = [PDF_POST, EVENT_FORM_POST, SIGNUP_POST, NEWSLETTER_POST, FLYER_POST, CLASS_POST]
THREE_MESSAGES = "chat_three_messages"
TWO_MESSAGES = "chat_with_image"


def load(name: str) -> BeautifulSoup:
    return BeautifulSoup((FIXTURES / f"{name}.html").read_text(encoding="utf-8"), "html.parser")


def norm(text: str | None) -> str:
    return " ".join((text or "").split())


def without_toggle(text: str | None) -> str:
    """Chrome counts the "Read Less" toggle as text; the parsers drop it as UI chrome."""
    return norm(text).removesuffix("Read Less").strip()


def browser_links(anchors: list[dict]) -> set[tuple[str, str]]:
    """Chrome's anchors minus what element_links documents it skips."""
    out = set()
    for a in anchors:
        href = a["href"] or ""
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if "fonts.gstatic.com" in href and "/notoemoji/" in href:
            continue
        if norm(a["text"]) in ("Read More", "Read Less"):
            continue
        out.add((norm(a["text"]) or href, urljoin("https://www.parentsquare.com/", href)))
    return out


# --- W1: feed-shaped pages parse at any nesting depth --------------------------------


def _feed_ids(soup: BeautifulSoup) -> set[int]:
    return {int(d["id"][5:]) for d in soup.find_all("div", id=re.compile(r"^feed_\d+$"))}


def test_live_feed_layout_parses_every_post():
    soup = load("feed_page")
    assert soup.select("#feeds-list > ul.feeds-list > li.feeds-list-item > div.ps-box"), "fixture is the live layout"
    posts = parse_feed_page(soup)
    assert len(posts) == 10
    assert {p.id for p in posts} == _feed_ids(soup)
    assert all(p.title and p.author and p.posted_at for p in posts)


def _old_layout(soup: BeautifulSoup) -> BeautifulSoup:
    """The pre-2026 layout: div.ps-box as a direct child of #feeds-list."""
    for li in soup.select("#feeds-list li.feeds-list-item"):
        li.unwrap()
    for ul in soup.select("#feeds-list ul.feeds-list"):
        ul.unwrap()
    return soup


def test_old_feed_layout_still_parses():
    soup = _old_layout(load("feed_page"))
    assert soup.select("#feeds-list > div.ps-box")
    assert not soup.select("li.feeds-list-item")
    live = [(p.id, p.title, p.summary) for p in parse_feed_page(load("feed_page"))]
    old = [(p.id, p.title, p.summary) for p in parse_feed_page(soup)]
    assert old == live


def test_posts_are_deduplicated_by_feed_id():
    soup = load("feed_page")
    first = soup.select_one("li.feeds-list-item")
    first.parent.append(BeautifulSoup(str(first), "html.parser"))
    assert len(parse_feed_page(soup)) == 10


@pytest.mark.parametrize("name,count,parse", [
    ("group_feed", 4, parse_group_feed),
    ("signups_page", 1, parse_feed_page),
    ("forms_empty", 0, parse_feed_page),
])
def test_other_feed_shaped_pages(name, count, parse):
    assert len(parse(load(name))) == count


def test_empty_polls_page_is_empty_not_an_error():
    assert parse_polls_page(load("polls_empty")) == []


def test_polls_parse_at_any_depth():
    """No live account had a poll; graft poll markup into a live post box."""
    soup = load("feed_page")
    box = soup.select_one("li.feeds-list-item div.ps-box")
    options = BeautifulSoup(
        '<div class="poll-option"><input type="radio" aria-label="Pizza"><span class="vote-progress-bar most"></span>'
        '<span class="num-votes">7 votes</span></div>'
        '<div class="poll-option"><input type="radio" aria-label="Tacos"><span class="num-votes">3 votes</span></div>',
        "html.parser")
    box.find("div", class_="feed-show").append(options)
    polls = parse_polls_page(soup)
    poll = next(p for p in polls if p.options)
    assert [(o.text, o.votes, o.is_winner) for o in poll.options] == [("Pizza", 7, True), ("Tacos", 3, False)]
    assert poll.total_votes == 10 and poll.posted_at is not None


# --- W3/W4: text equals Chrome's innerText, links equal its anchors -------------------


@pytest.mark.parametrize("name", POSTS)
def test_post_body_equals_browser_inner_text(name):
    post = parse_post_detail(load(name))
    assert norm(post.body_text) == norm(EXPECTED[name]["detail"]["text"])


@pytest.mark.parametrize("name", POSTS)
def test_post_links_equal_browser_anchors(name):
    post = parse_post_detail(load(name))
    got = {(norm(link.text), link.href) for link in post.links}
    assert got == browser_links(EXPECTED[name]["detail"]["anchors"])


def test_body_keeps_line_and_paragraph_breaks():
    body = parse_post_detail(load(PDF_POST)).body_text
    assert "\n" in body
    # 0.4.0 joined text nodes with nothing between them ("…below:🔗https://…")
    assert not re.search(r"[a-z][.!?:,][A-Z]", body)


def test_emoji_glyph_anchors_are_not_links():
    anchors = EXPECTED[PDF_POST]["detail"]["anchors"]
    assert any("notoemoji" in a["href"] for a in anchors), "the fixture has emoji-glyph anchors"
    assert not any("notoemoji" in link.href for link in parse_post_detail(load(PDF_POST)).links)


@pytest.mark.parametrize("name", ["feed_page", "group_feed", "signups_page"])
def test_feed_summaries_equal_browser_inner_text(name):
    posts = {f"feed_{p.id}": p for p in parse_feed_page(load(name))}
    expected = EXPECTED[name]["posts"]
    assert posts.keys() == expected.keys()
    for key, post in posts.items():
        assert norm(post.summary) == norm(expected[key]["text"]), key
        assert {(norm(link.text), link.href) for link in post.links} == browser_links(expected[key]["anchors"]), key


@pytest.mark.parametrize("name", [THREE_MESSAGES, TWO_MESSAGES])
def test_chat_text_and_links_equal_browser(name):
    messages = parse_chat_thread(load(name))
    expected = EXPECTED[name]["messages"]
    assert [f"chat-message-{m.id}" for m in messages] == [e["id"] for e in expected]
    for m, e in zip(messages, expected):
        assert norm(m.text) == norm(e["text"])
        assert {(norm(link.text), link.href) for link in m.links} == browser_links(e["anchors"])


def test_notice_bodies_equal_browser_inner_text():
    notices = parse_notices(load("notices"))
    expected = EXPECTED["notices"]["notices"]
    assert [f"notice-{n.notice_type}-{n.id}" for n in notices] == [e["id"] for e in expected]
    for n, e in zip(notices, expected):
        assert norm(n.text_message) == norm(e["text"])
        assert norm(n.body) == without_toggle(e["email"] if e["email"] is not None else e["text"])
        assert {(norm(link.text), link.href) for link in n.links} == browser_links(e["anchors"])


# --- W5: comments at this school are private -----------------------------------------


@pytest.mark.parametrize("name", [PDF_POST, NEWSLETTER_POST, EVENT_FORM_POST])
def test_private_comments_parse_as_none_with_the_page_count(name):
    post = parse_post_detail(load(name))
    assert post.comments == []
    assert post.comment_count == 0


# --- W6: notices carry ids, bodies and timestamps ------------------------------------


def test_notices_have_stable_ids_bodies_and_timestamps():
    notices = parse_notices(load("notices"), school_id=IDS["school_id"], tz=EASTERN, now=NOW)
    assert [n.notice_type for n in notices] == ["alert", "alert"]
    assert all(n.id for n in notices) and len({n.id for n in notices}) == 2
    assert notices[0].url == f"https://www.parentsquare.com/schools/{IDS['school_id']}/notices#notice-alert-{notices[0].id}"
    assert notices[0].subject and notices[0].body and notices[0].text_message
    assert notices[0].body != notices[0].text_message
    assert notices[1].body == notices[1].text_message  # text-only alert
    assert [n.posted_at for n in notices] == [
        datetime(2026, 9, 8, 20, 59, tzinfo=EASTERN),
        datetime(2026, 9, 18, 18, 0, tzinfo=EASTERN),
    ]


# --- W13/W14: timestamps, message ids, direction, group flag -------------------------


def test_feed_posts_have_utc_timestamps():
    for p in parse_feed_page(load("feed_page")):
        assert p.posted_at.tzinfo is not None
        assert p.posted_at == datetime.fromisoformat(p.date)


def test_chat_messages_have_ids_direction_and_exact_times():
    messages = parse_chat_thread(load(THREE_MESSAGES), tz=EASTERN, now=NOW)
    assert all(m.chat_id == IDS[THREE_MESSAGES] for m in messages)
    assert [m.is_mine for m in messages] == [True, False, True]
    assert all(m.id for m in messages) and len({m.id for m in messages}) == 3
    assert [m.posted_at for m in messages] == [
        datetime(2026, 9, 10, 6, 38, tzinfo=EASTERN),
        datetime(2026, 9, 10, 8, 13, tzinfo=EASTERN),
        datetime(2026, 9, 10, 8, 29, tzinfo=EASTERN),
    ]


def test_chat_year_comes_from_the_weekday():
    """"Mon, Aug 24" is 2026 seen from Sep 2026, and 2020 seen from 2025."""
    soup = load(TWO_MESSAGES)
    assert parse_chat_thread(soup, tz=EASTERN, now=NOW)[0].posted_at.year == 2026
    assert parse_chat_thread(soup, tz=EASTERN, now=datetime(2025, 6, 1, tzinfo=timezone.utc))[0].posted_at.year == 2020


def test_conversation_list_fields():
    convos = parse_conversation_list(load("chat_list"), tz=EASTERN)
    assert [c.message_count for c in convos] == [3, 2]
    assert [c.is_group for c in convos] == [False, False]
    assert convos[0].last_message_at == datetime(2026, 9, 10, 8, 29, tzinfo=EASTERN)
    assert {c.id for c in convos} == {IDS[THREE_MESSAGES], IDS[TWO_MESSAGES]}


def test_group_conversation_is_flagged():
    soup = load("chat_list")
    icon = soup.find(attrs={"data-testid": "chat-thread-type-icon"})
    icon["class"] = ["fa", "fa-fw", "fa-users"]
    assert [c.is_group for c in parse_conversation_list(soup)] == [True, False]


# --- W15: full-size images, alt text, original filenames -----------------------------


def test_pdf_attachment_from_the_live_attachment_link():
    post = parse_post_detail(load(PDF_POST))
    docs = [a for a in post.attachments if a.file_type == "document"]
    assert len(docs) == 1
    assert docs[0].name.endswith(".pdf")
    assert re.fullmatch(r"https://www\.parentsquare\.com/feeds/\d+/attachment/\d+", docs[0].url)


def test_inline_images_are_full_size_with_original_names():
    post = parse_post_detail(load(FLYER_POST))
    images = [a for a in post.attachments if a.file_type == "image"]
    assert len(images) == 2
    assert all(a.url.startswith("https://posts.parentsquare.com/inline_images_") for a in images)
    # the upload prefix ("inline_images_<hex>_<epoch ms>-") is stripped, "+" means a space
    assert all(not a.name.startswith("inline_images_") and "+" not in a.name for a in images)
    assert all(re.search(r"\.(png|jpe?g|gif)$", a.name) for a in images)


def test_image_alt_text_is_kept():
    post = parse_post_detail(load(EVENT_FORM_POST))
    assert [bool(a.alt) for a in post.attachments if a.file_type == "image"] == [True]


def test_chat_image_is_the_viewers_full_size_not_the_thumbnail():
    message = parse_chat_thread(load(TWO_MESSAGES))[0]
    [image] = [a for a in message.attachments if a.file_type == "image"]
    assert "/full_" in image.url and "thumb_" in (image.thumbnail_url or "")
    assert image.alt and image.name == image.alt
    assert [a for a in message.attachments if a.file_type == "document"] == []  # its download link is the same file


# --- W16: kinds, event, form, sign-up state ------------------------------------------


def test_event_and_form_details():
    post = parse_post_detail(load(EVENT_FORM_POST))
    assert post.kinds == ["event", "form"] and not post.is_pinned
    assert post.event.start == datetime(2026, 9, 24, 22, 30, tzinfo=timezone.utc)
    assert post.event.end == datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
    assert post.event.label.startswith("Sep 24, Thursday") and " To " in post.event.label
    assert re.fullmatch(r"https://www\.parentsquare\.com/feeds/\d+/add_cal\.ics\?district_id=\d+", post.event.ics_url)
    assert post.form.due.startswith("Complete by Wednesday, Sep 23")
    assert post.form.closed is True
    assert post.form.signed is False  # answer fields exist and are all empty
    assert len(post.form.questions) == 4


def test_post_ids_come_from_the_page():
    for name in POSTS:
        assert parse_post_detail(load(name)).id == IDS[name]


def test_signup_slots_carry_day_and_state():
    post = parse_post_detail(load(SIGNUP_POST))
    assert post.kinds == ["signup"]
    slots = post.signup_items
    assert len(slots) == 21
    assert [s.date for s in slots].count("Thursday, Oct 1") + [s.date for s in slots].count("Friday, Oct 2") == 21
    assert sorted({s.status for s in slots}) == ["closed", "open"]
    assert sum(s.status == "open" for s in slots) == 2
    assert not any(s.mine for s in slots)


def test_signup_progress_in_the_feed():
    [post] = parse_feed_page(load("signups_page"))
    assert post.signup_progress == "19/21 Sign Ups"


# --- W17: student dashboard -----------------------------------------------------------


def test_student_dashboard_live_layout():
    d = parse_student_dashboard(load("student_dashboard"))
    assert d.student_name and d.grade == "Kindergarten"
    assert d.school_id == IDS["school_id"]
    assert d.student_id == IDS["student_id"]
    assert len(d.classes) == 1 and d.classes[0].startswith("Kindergarten-")
    [section] = d.sections
    [teacher] = section.teachers
    assert teacher.name and teacher.user_id
    assert d.teachers == [teacher.name]
