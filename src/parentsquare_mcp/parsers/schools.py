from __future__ import annotations

from bs4 import BeautifulSoup


def parse_sidebar_features(soup: BeautifulSoup) -> list[str]:
    """Extract feature list from sidebar navigation on any school page.

    The sidebar has sections like COMMUNICATE, EXPLORE, PARTICIPATE with
    links to different features. Returns a list of feature names.
    """
    features: list[str] = []

    # Look for sidebar navigation links
    # ParentSquare sidebar uses <a> tags within nav sections
    sidebar = soup.find("div", class_="sidebar") or soup.find("nav", class_="sidebar")
    if sidebar:
        for link in sidebar.find_all("a"):
            text = link.get_text(strip=True)
            if text and text not in ("", "Home"):
                features.append(text)
        return features

    # Fallback: look for left-nav links with specific patterns
    for link in soup.select(".left-nav a, .side-nav a, .navigation a, [data-nav] a"):
        text = link.get_text(strip=True)
        if text and len(text) > 1:
            features.append(text)

    if not features:
        # Last resort: find all nav-like links
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if any(section in href for section in ["/feeds", "/chats", "/calendars", "/photos",
                                                    "/videos", "/files", "/users", "/groups",
                                                    "/volunteer", "/notices", "/sign_ups", "/links"]):
                text = link.get_text(strip=True)
                if text and text not in features:
                    features.append(text)

    return features
