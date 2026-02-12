from __future__ import annotations

from bs4 import BeautifulSoup

from parentsquare_mcp.models import DirectoryEntry


def parse_directory(soup: BeautifulSoup) -> list[DirectoryEntry]:
    """Parse /schools/{id}/users -> staff directory.

    Structure:
      #school-staff-panel table tbody tr
        th.cell-one span.cell-one-name  (name: "Last, First")
        td.cell-two  (role: Staff/Teacher)
        td.cell-four  (virtual phone)

    Skip the hidden template row (#panel-table-row).
    """
    entries: list[DirectoryEntry] = []

    staff_panel = soup.find("div", id="school-staff-panel")
    if not staff_panel:
        # Fallback: try any panel with a staff table
        staff_panel = soup.find("table", class_="table")
        if staff_panel:
            staff_panel = staff_panel.parent

    if not staff_panel:
        return entries

    table = staff_panel.find("table", class_="table")
    if not table:
        return entries

    tbody = table.find("tbody")
    if not tbody:
        return entries

    for row in tbody.find_all("tr"):
        # Skip hidden template row
        if row.get("id") == "panel-table-row":
            continue
        if row.get("style") and "display:none" in row["style"].replace(" ", ""):
            continue
        # Skip empty message rows
        if row.find("td", class_="empty-table-message"):
            continue

        # Name
        name_span = row.find("span", class_="cell-one-name")
        if not name_span:
            continue
        name = name_span.get_text(strip=True)

        # Convert "Last, First" to "First Last"
        if ", " in name:
            parts = name.split(", ", 1)
            name = f"{parts[1]} {parts[0]}"

        # Role
        role_td = row.find("td", class_="cell-two")
        role = role_td.get_text(strip=True) if role_td else ""

        # Virtual phone
        phone_td = row.find("td", class_="cell-four")
        phone = phone_td.get_text(strip=True) if phone_td else None
        if phone == "":
            phone = None

        entries.append(
            DirectoryEntry(
                name=name,
                role=role,
                email=None,  # Not shown in directory table
                phone=phone,
            )
        )

    return entries
