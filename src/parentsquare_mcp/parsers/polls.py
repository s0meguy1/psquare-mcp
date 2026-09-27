from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString

from parentsquare_mcp.models import Poll, PollOption
from parentsquare_mcp.parsers.dates import parse_timestamp
from parentsquare_mcp.parsers.feeds import check_drift, count_feed_markers, iter_post_boxes
from parentsquare_mcp.parsers.text import element_text, one_line


def parse_polls_page(soup: BeautifulSoup) -> list[Poll]:
    """Parse /schools/{id}/polls -> list of Poll.

    Polls reuse the feeds-list structure, so polls are found at any depth
    (see ``feeds.iter_post_boxes``; W1). Each poll box contains:
      div#feed_{id} — poll feed ID
      .feed-title .subject a — poll question (no role=heading on polls)
      .feed-metadata .user-name — author
      .feed-metadata .time-ago — date
      div.poll-option — one per answer choice, containing:
        input[aria-label] — option text
        span.vote-progress-bar — bar with 'most' class on winner
        div.vote-progress-text — option text (fallback)
        span.num-votes — vote count

    Raises ``ParseDriftError`` when the page holds polls but none parse.
    """
    polls: list[Poll] = []
    for feed_id, ps_box in iter_post_boxes(soup):

        # Question text — polls use <a> inside .subject, not span[role=heading]
        heading = ps_box.find(attrs={"role": "heading"})
        if heading:
            question = one_line(element_text(heading))
        else:
            subject = ps_box.find("div", class_="subject")
            link = subject.find("a") if subject else None
            question = one_line(element_text(link)) if link else ""

        # Author
        author_el = ps_box.find("a", class_="user-name")
        author = one_line(element_text(author_el)) if author_el else ""

        # Date — polls lack time-ago spans; date is a bare text node in .feed-metadata
        # Format: "• 8 months • Tuesday, Jul 1 at 9:50 AM •"
        time_el = ps_box.find("span", class_="time-ago")
        date = ""
        if time_el:
            date = time_el.get("data-timestamp", "") or one_line(element_text(time_el))
        if not date:
            metadata = ps_box.find(class_="feed-metadata")
            if metadata:
                for child in metadata.children:
                    if isinstance(child, NavigableString):
                        text = child.strip().strip("•").strip()
                        # Look for "DayName, Month Day at HH:MM AM/PM"
                        m = re.search(r"(\w+day,\s+\w+\s+\d+\s+at\s+\d+:\d+\s+[AP]M)", text)
                        if m:
                            date = m.group(1)
                            break

        # Poll options
        options: list[PollOption] = []
        total_votes = 0
        for opt_div in ps_box.find_all("div", class_="poll-option"):
            # Option text — from input aria-label or vote-progress-text
            radio = opt_div.find("input", attrs={"aria-label": True})
            text = radio["aria-label"] if radio else ""
            if not text:
                prog_text = opt_div.find("div", class_="vote-progress-text")
                text = one_line(element_text(prog_text)) if prog_text else ""

            # Vote count
            num_el = opt_div.find("span", class_="num-votes")
            votes = 0
            if num_el:
                m = re.search(r"(\d+)", num_el.get_text(strip=True))
                if m:
                    votes = int(m.group(1))

            # Winner indicator
            bar = opt_div.find("span", class_="vote-progress-bar")
            is_winner = bar is not None and "most" in " ".join(bar.get("class", []))

            total_votes += votes
            options.append(PollOption(text=text, votes=votes, is_winner=is_winner))

        # Detect if user already voted
        voted_text = ps_box.get_text()
        user_voted = "already voted" in voted_text.lower()

        if not (question or options):
            continue
        polls.append(Poll(
            id=feed_id,
            question=question,
            author=author,
            date=date,
            options=options,
            total_votes=total_votes,
            user_voted=user_voted,
            posted_at=parse_timestamp(date),
        ))

    check_drift("polls page", count_feed_markers(soup), len(polls), "polls")
    return polls
