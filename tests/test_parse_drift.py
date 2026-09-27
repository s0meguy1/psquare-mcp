"""W2: a page that clearly holds items but parses to nothing raises ParseDriftError.

Before, every parser returned ``[]`` both for an empty page and for markup it no
longer understood, so drift looked exactly like a quiet day. Each test renames
the classes a parser keys on in a live (scrubbed) fixture and expects the
error, and checks that a genuinely empty page still returns ``[]``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from parentsquare_mcp.errors import ParseDriftError
from parentsquare_mcp.parsers.feeds import parse_feed_page, parse_post_detail
from parentsquare_mcp.parsers.groups import parse_groups_list
from parentsquare_mcp.parsers.messages import parse_chat_thread, parse_conversation_list
from parentsquare_mcp.parsers.notices import parse_notices
from parentsquare_mcp.parsers.polls import parse_polls_page

FIXTURES = Path(__file__).parent / "fixtures" / "live_2026_09"


def load(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def renamed(html: str, *pairs: tuple[str, str]) -> BeautifulSoup:
    """Rename class tokens / attribute values the way a site redesign would."""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.find_all(True):
        if el.get("class"):
            new = []
            for c in el["class"]:
                for old, repl in pairs:
                    c = re.sub(old, repl, c)
                new.append(c)
            el["class"] = new
        for attr in ("role", "data-testid"):
            if el.get(attr):
                for old, repl in pairs:
                    el[attr] = re.sub(old, repl, el[attr])
    return soup


FEED_REDESIGN = (
    (r"^heading$", "title-text"), (r"^user-name$", "byline"), (r"^time-ago$", "when"),
    (r"^description$", "body"), (r"^feed-metadata-from$", "sender"), (r"^feed$", "card"),
)


def test_feed_with_renamed_classes_raises():
    soup = renamed(load("feed_page"), *FEED_REDESIGN)
    # The post ids are still there, so the page clearly holds 10 posts...
    assert len(soup.find_all("div", id=re.compile(r"^feed_\d+$"))) == 10
    # ...but nothing a post is made of can be found any more.
    with pytest.raises(ParseDriftError) as exc:
        parse_feed_page(soup, page="feed page 1")
    assert exc.value.page == "feed page 1" and exc.value.markers == 10
    assert isinstance(exc.value, RuntimeError)


@pytest.mark.parametrize("name", ["forms_empty", "polls_empty"])
def test_genuinely_empty_pages_do_not_raise(name):
    soup = BeautifulSoup(load(name), "html.parser")
    assert parse_feed_page(soup) == []
    assert parse_polls_page(soup) == []


def test_polls_page_with_renamed_classes_raises():
    soup = renamed(load("feed_page"), *FEED_REDESIGN, (r"^subject", "headline"))
    with pytest.raises(ParseDriftError):
        parse_polls_page(soup)


def test_conversation_list_with_renamed_links_raises():
    soup = BeautifulSoup(load("chat_list"), "html.parser")
    for a in soup.find_all("a", class_=re.compile("a-chat-thread")):
        a["href"] = "#"          # no /chats/<id> any more
        a["class"] = ["thread-row"]
        a["data-testid"] = "chat-thread-item-x"
    soup.find(id="chat-threads-container").append(
        BeautifulSoup('<a class="a-chat-thread" href="#">?</a>', "html.parser"))
    with pytest.raises(ParseDriftError):
        parse_conversation_list(soup)


def test_chat_thread_with_renamed_classes_raises():
    soup = renamed(load("chat_three_messages"),
                   (r"^chat-message-meta$", "meta"), (r"^user$", "who"), (r"^chat-message$", "bubble"),
                   (r"^chat-message-timestamp$", "stamp"))
    assert soup.find_all("div", id=re.compile(r"^chat-message-\d+$"))
    with pytest.raises(ParseDriftError):
        parse_chat_thread(soup)


def test_chat_thread_panel_is_not_a_message():
    """div#chat-message-details (the thread panel) matches id^=chat-message- but is not a message."""
    soup = BeautifulSoup(load("chat_three_messages"), "html.parser")
    soup.find(id="chat-messages").insert(0, BeautifulSoup('<div id="chat-message-details">x</div>', "html.parser"))
    assert len(parse_chat_thread(soup)) == 3


def test_notices_with_renamed_classes_raise():
    soup = renamed(load("notices"), (r"^notice-type-", "kind-"), (r"^notice-title$", "headline"),
                   (r"^feed-metadata$", "meta"))
    for el in soup.find_all(id=re.compile(r"^smart-alert-")):
        el["id"] = el["id"].replace("smart-alert", "sa")
    with pytest.raises(ParseDriftError):
        parse_notices(soup)


def test_post_page_with_nothing_parseable_raises():
    soup = renamed(load("post_pdf"), *FEED_REDESIGN, (r"^subject", "headline"))
    with pytest.raises(ParseDriftError):
        parse_post_detail(soup)


def test_groups_page_shell_raises_with_guidance():
    """The groups page is served as an empty React shell; parsing it used to give []."""
    with pytest.raises(ParseDriftError, match="list_groups"):
        parse_groups_list(BeautifulSoup(load("groups_shell"), "html.parser"))


def test_partial_loss_is_logged_not_raised(caplog):
    soup = BeautifulSoup(load("feed_page"), "html.parser")
    victim = soup.find("div", id=re.compile(r"^feed_\d+$"))
    for el in victim.find_all(True):
        el.attrs.pop("class", None)
        el.attrs.pop("role", None)
    for text in victim.find_all(string=True):
        text.replace_with("")
    victim.parent.attrs.pop("aria-label", None)
    with caplog.at_level("WARNING"):
        posts = parse_feed_page(soup, page="feed page 1")
    assert len(posts) == 9
    assert "parsed 9 of 10 posts" in caplog.text and "feed page 1" in caplog.text
