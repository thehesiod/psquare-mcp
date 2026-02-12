from __future__ import annotations

import re

from bs4 import BeautifulSoup

from parentsquare_mcp.models import Notice


def parse_notices(soup: BeautifulSoup) -> list[Notice]:
    """Parse /schools/{id}/notices -> list of Notice.

    Notice structure:
      div.ps-box.notice-type-{alert|document}
        .notice-title-icon-container > .feed-title > span.notice-title > a  (title)
        .feed-metadata > span  (date, school)
        .tab-content > .tab-pane  (body text)
    """
    notices: list[Notice] = []

    for box in soup.find_all("div", class_="ps-box"):
        classes = " ".join(box.get("class", []))

        # Determine notice type from CSS class
        if "notice-type-alert" in classes:
            notice_type = "alert"
        elif "notice-type-document" in classes:
            notice_type = "document"
        else:
            continue

        # Title
        title_el = box.find("span", class_="notice-title")
        title = title_el.get_text(strip=True) if title_el else ""
        # Strip "Alert:" or "Document:" prefix
        title = re.sub(r"^(Alert|Document):\s*", "", title)

        # Date and school from feed-metadata spans
        date = ""
        school = ""
        metadata = box.find("div", class_="feed-metadata")
        if metadata:
            spans = metadata.find_all("span", recursive=False)
            # First span is date, "text-dark" span is school name
            for span in spans:
                text = span.get_text(strip=True)
                if not text or text == "•":
                    continue
                if "text-dark" in " ".join(span.get("class", [])):
                    school = text
                elif not date:
                    date = text

        notices.append(Notice(
            title=title,
            notice_type=notice_type,
            date=date,
            school=school,
        ))

    return notices
