from __future__ import annotations

from bs4 import BeautifulSoup

from parentsquare_mcp.models import SchoolLink


def parse_links_page(soup: BeautifulSoup) -> list[SchoolLink]:
    """Parse /schools/{id}/links -> list of SchoolLink.

    Links page structure:
      div.ps-box > table.table > tr
        td:first — link name (inside <a>)
        td:second — school name
        a[href] — external URL (Google Drive, etc.)
    """
    links: list[SchoolLink] = []

    table = soup.find("table", class_="table")
    if not table:
        return links

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        anchor = cells[0].find("a", href=True)
        if not anchor:
            continue

        name = anchor.get_text(strip=True)
        url = anchor["href"]
        school = cells[1].get_text(strip=True)

        if name and url:
            links.append(SchoolLink(name=name, url=url, school=school))

    return links
