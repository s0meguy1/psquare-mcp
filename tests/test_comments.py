"""W5: comments parse wherever they sit, with author, time and text kept apart.

The account these fixtures come from belongs to a school whose comments are
private ("Only you and <author> can view"), so no live post had a comment to
capture. The markup below is therefore built from the selectors ParentSquare's
own JavaScript uses on comments (``.comment``, ``.comment-container``,
``a.comment-full-name``, ``.comment-content-original``/``-translated``,
``.replies``/``.comment-reply``, ``.delete-comment``, ``.reply-link``), plus the
older bare ``div`` layout 0.4.0 documented. The live private-comment pages are
covered in test_live_fixtures.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from parentsquare_mcp.parsers.feeds import parse_comments, parse_post_detail

COMMENTS = """
<div class="comments-box">
  <h3 class="comments-head">Comments &amp; Replies</h3>
  <div class="comment-count">3 comments on this post</div>
  <div class="comments">
    <div class="comment" id="comment_501">
      <div class="comment-container">
        <div class="avatar"><div class="initials">JD</div></div>
        <a class="comment-full-name" href="/schools/1/users/42">Jane Doe</a>
        <span class="time-ago" data-timestamp="2026-09-20 14:05:00+00:00">7 days ago</span>
        <div class="comment-content-original"><p>Thanks!<br>See <a href="https://example.org/form">the form</a>.</p></div>
        <div class="comment-content-translated" style="display:none">Gracias</div>
        <div class="delete-comment"><a href="#">Delete</a></div>
        <a class="reply-link" href="#">Reply</a>
      </div>
      <div class="replies">
        <div class="comment-reply" id="comment_502">
          <a class="comment-full-name" href="/schools/1/users/7">Ms. Teacher</a>
          <span class="time-ago" data-timestamp="2026-09-20 15:00:00+00:00">7 days ago</span>
          <div class="comment-content-original">You're welcome</div>
        </div>
      </div>
    </div>
    <div class="comment" id="comment_503">
      <div class="comment-container">
        <a class="comment-full-name" href="/schools/1/users/43">Sam Roe</a>
        <span class="time-ago" data-timestamp="2026-09-21 09:00:00+00:00">6 days ago</span>
        <div class="comment-content-original">Will there be snacks?</div>
      </div>
    </div>
  </div>
  <div class="comments-form"><form class="new_comment"><a href="/help">Private</a><textarea class="comment-content">draft</textarea></form></div>
</div>
"""

OLD_LAYOUT = """
<div class="comments-box"><div class="comments">
  <div><a class="user-name" href="/schools/1/users/42">Jane Doe</a> <span class="time-ago" data-timestamp="2026-09-19 10:00:00+00:00">8 days ago</span>
       <p>Great idea!</p><a href="#">Reply</a></div>
</div>
<div class="comments-form"><form><a href="/help">Private</a></form></div></div>
"""


def test_comments_with_replies_and_clean_text():
    comments = parse_comments(BeautifulSoup(COMMENTS, "html.parser"))
    assert [c.author for c in comments] == ["Jane Doe", "Sam Roe"]
    first = comments[0]
    assert first.id == 501
    assert first.text == "Thanks!\nSee the form."
    assert first.posted_at == datetime(2026, 9, 20, 14, 5, tzinfo=timezone.utc)
    assert [(link.text, link.href) for link in first.links] == [("the form", "https://example.org/form")]
    [reply] = first.replies
    assert (reply.id, reply.author, reply.text) == (502, "Ms. Teacher", "You're welcome")
    for c in (first, reply, comments[1]):
        for noise in ("Jane Doe", "days ago", "Delete", "Reply", "Gracias", "JD", "draft", "Private"):
            assert noise not in c.text


def test_older_bare_div_layout():
    [c] = parse_comments(BeautifulSoup(OLD_LAYOUT, "html.parser"))
    assert (c.author, c.text) == ("Jane Doe", "Great idea!")
    assert c.posted_at == datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)


def test_comments_inside_a_live_post_page():
    """Graft the comments into a real post page: they parse, and the page count is read."""
    fixture = Path(__file__).parent / "fixtures" / "live_2026_09" / "post_pdf.html"
    soup = BeautifulSoup(fixture.read_text(encoding="utf-8"), "html.parser")
    box = soup.find("div", class_="comments-box")
    box.replace_with(BeautifulSoup(COMMENTS, "html.parser"))
    post = parse_post_detail(soup)
    assert post.comment_count == 3
    assert len(post.comments) == 2 and len(post.comments[0].replies) == 1


def test_no_comments_is_empty():
    empty = '<div class="comments-box"><div class="comment-count">0 comments on this post</div><div class="comments"></div></div>'
    assert parse_comments(BeautifulSoup(empty, "html.parser")) == []
