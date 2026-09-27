from __future__ import annotations

import re

import json

from bs4 import BeautifulSoup

from parentsquare_mcp.errors import ParseDriftError
from parentsquare_mcp.models import FeedPost, Group
from parentsquare_mcp.parsers.text import element_text, one_line


def parse_groups_list(soup: BeautifulSoup) -> list[Group]:
    """Parse /schools/{id}/groups -> list of Group.

    This is a React-rendered page with Tailwind CSS classes.
    Strategy: Find group links by href pattern /groups/{id}/feeds and
    extract surrounding data (name, member count, etc.)

    Structure:
      #react-app-root
        a[href*=/groups/][href*=/feeds]  (group name links)
        a[href*=/groups/][href*=/users]  (member count links, text="{N} Users")

    Only a *rendered* page has those links. The HTML the server sends is an
    empty React shell (``#react-app-root`` with ``"ns": "groups"`` in its
    ``data-app-context``) that fills itself in over GraphQL, so parsing the
    fetched page used to return ``[]`` every time. That now raises
    ``ParseDriftError``; use ``PSClient.list_groups(school_id)``, which asks
    the same GraphQL API the page does.
    """
    groups: list[Group] = []

    # Find all group name links
    group_links = soup.find_all("a", href=re.compile(r"/groups/\d+/feeds"))
    seen_ids: set[int] = set()

    for link in group_links:
        href = link.get("href", "")
        id_match = re.search(r"/groups/(\d+)/feeds", href)
        if not id_match:
            continue
        group_id = int(id_match.group(1))
        if group_id in seen_ids:
            continue
        seen_ids.add(group_id)

        name = one_line(element_text(link))

        # Find the containing element for this group
        # Walk up to find sibling/nearby elements with stats
        container = link
        for _ in range(8):
            if container.parent:
                container = container.parent
            else:
                break

        # Member count — look for users link nearby
        member_count = 0
        users_link = container.find("a", href=re.compile(rf"/groups/{group_id}/users"))
        if users_link:
            count_match = re.search(r"(\d+)\s*Users?", users_link.get_text())
            if count_match:
                member_count = int(count_match.group(1))

        # Description — not always present, look for descriptive text nearby
        description = None

        groups.append(
            Group(
                id=group_id,
                name=name,
                member_count=member_count,
                description=description,
            )
        )

    if not groups:
        root = soup.find(id="react-app-root")
        context = {}
        if root is not None:
            try:
                context = json.loads(root.get("data-app-context") or "{}")
            except ValueError:
                context = {}
        if root is not None and context.get("ns") == "groups":
            raise ParseDriftError(
                "groups page",
                "the server sends an empty React shell that loads groups over GraphQL; "
                "use PSClient.list_groups(school_id) instead of parsing this page",
            )
    return groups


def parse_group_feed(soup: BeautifulSoup) -> list[FeedPost]:
    """Parse /schools/{id}/groups/{group_id}/feeds.

    Group feeds use the same structure as the main feed.
    Reuse the feed parser.
    """
    from parentsquare_mcp.parsers.feeds import parse_feed_page

    return parse_feed_page(soup, page="group feed")


# The query the groups page itself sends (captured from the page, 2026-09).
# userCount/activeFeedsCount/hasUserOrStudent were removed from the Group type,
# so asking for them makes the whole query 422.
GROUPS_QUERY = """
query GetGroups($institute: InstituteInputType!, $studentId: ID = null) {
  groupsIndex(institute: $institute, studentId: $studentId) {
    list {
      instituteName
      hasGroups
      categorizedGroups {
        name
        groups {
          id
          name
          description
          isPublic
        }
      }
    }
  }
}
"""


def groups_from_graphql(data: dict, post_counts: dict[int, int] | None = None) -> list[Group]:
    """Map a ``GetGroups`` response (plus optional post counts by id) to Groups."""
    post_counts = post_counts or {}
    cat_groups = (data.get("groupsIndex") or {}).get("list", {}).get("categorizedGroups", [])
    groups: list[Group] = []
    for cat in cat_groups:
        cat_name = cat.get("name", "")
        for g in cat.get("groups", []):
            gid = g["id"]
            groups.append(Group(
                id=int(gid),
                name=g["name"],
                member_count=0,  # userCount removed from GraphQL Group type
                description=g.get("description"),
                category=cat_name,
                post_count=post_counts.get(int(gid), 0),
                is_member=False,  # hasUserOrStudent removed from GraphQL Group type
            ))
    return groups
