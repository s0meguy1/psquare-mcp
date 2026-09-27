"""W3/W4: the innerText rules element_text follows, and what element_links keeps.

The live pages are checked against Chrome in test_live_fixtures.py; these pin
the individual rules on small inputs.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from parentsquare_mcp.parsers.text import element_links, element_text


def text(html: str) -> str:
    return element_text(BeautifulSoup(f"<div>{html}</div>", "html.parser").div)


def links(html: str) -> list[tuple[str, str]]:
    return [(link.text, link.href) for link in element_links(BeautifulSoup(f"<div>{html}</div>", "html.parser").div)]


@pytest.mark.parametrize("html,expected", [
    # the 0.4.0 bug: "Community Members,We would like…"
    ("Community Members,<br>We would like", "Community Members,\nWe would like"),
    ("<p>One</p><p>Two</p>", "One\n\nTwo"),
    ("<div>One</div><div>Two</div>", "One\nTwo"),
    ("<ul><li>Milk</li><li>Eggs</li></ul>", "Milk\nEggs"),
    ("<ul><li><p>Milk</p></li><li><p>Eggs</p></li></ul>", "Milk\n\nEggs"),
    ("<b>Bold</b><i>Italic</i> text", "BoldItalic text"),       # inline: no separator
    ("Hello <b> world </b>!", "Hello world !"),
    ("  lots   of \n\n  space  ", "lots of space"),
    ("<div>\n  <p>A</p>\n  \n  <p>B</p>\n</div>", "A\n\nB"),    # whitespace between blocks
    ("<p>A</p><br><br><br><br><p>B</p>", "A\n\nB"),             # at most one blank line
    ("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>", "a\tb\nc\td"),
    ("<pre>  keep\n    this</pre>", "keep\nthis"),                # lines are trimmed after extraction
    ("<span style='white-space:pre-wrap'>a  b</span>", "a b"),
    ("x&nbsp;y", "x y"),
    ("<p>shown</p><p style='display: none'>hidden</p>", "shown"),
    ("<p>shown</p><p class='hidden'>hidden</p><p hidden>also</p>", "shown"),
    ("<p>shown</p><div class='hidden-sm hidden-md hidden-lg'>mobile only</div>", "shown"),
    ("<p>shown</p><div class='hidden-xs'>desktop</div>", "shown\n\ndesktop"),  # a <p> sets off a blank line
    ("<p>A<script>var x = 1;</script><style>p{}</style></p>", "A"),
    ('<img alt="flyer"> <p>caption</p>', "caption"),
    ("", ""),
])
def test_inner_text_rules(html, expected):
    assert text(html) == expected


def test_links_are_absolute_and_skip_page_chrome():
    html = """
      <a href="https://example.org/signup">Sign up here</a>
      <a href="/feeds/123">relative</a>
      <a href="#">Read More</a>
      <a href="javascript:void(0)">js</a>
      <a class="description-link" href="/feeds/123">Read More</a>
      <a href="https://fonts.gstatic.com/s/e/notoemoji/17.0/1f517/32.png">🔗</a>
      <a href="mailto:office@example.org">office@example.org</a>
      <a href="https://example.org/signup">Sign up here</a>
      <a href="https://example.org/icon" aria-label="Calendar"><img src="c.png"></a>
      <p style="display:none"><a href="https://example.org/hidden">hidden</a></p>
    """
    assert links(html) == [
        ("Sign up here", "https://example.org/signup"),
        ("relative", "https://www.parentsquare.com/feeds/123"),
        ("office@example.org", "mailto:office@example.org"),
        ("Calendar", "https://example.org/icon"),
    ]
