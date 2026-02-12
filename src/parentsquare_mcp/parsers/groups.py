from __future__ import annotations

import re

from bs4 import BeautifulSoup

from parentsquare_mcp.models import FeedPost, Group


def parse_groups_list(soup: BeautifulSoup) -> list[Group]:
    """Parse /schools/{id}/groups -> list of Group.

    This is a React-rendered page with Tailwind CSS classes.
    Strategy: Find group links by href pattern /groups/{id}/feeds and
    extract surrounding data (name, member count, etc.)

    Structure:
      #react-app-root
        a[href*=/groups/][href*=/feeds]  (group name links)
        a[href*=/groups/][href*=/users]  (member count links, text="{N} Users")
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

        name = link.get_text(strip=True)

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

    return groups


def parse_group_feed(soup: BeautifulSoup) -> list[FeedPost]:
    """Parse /schools/{id}/groups/{group_id}/feeds.

    Group feeds use the same structure as the main feed.
    Reuse the feed parser.
    """
    from parentsquare_mcp.parsers.feeds import parse_feed_page

    return parse_feed_page(soup)
