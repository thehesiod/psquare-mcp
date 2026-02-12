from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from parentsquare_mcp.models import VolunteerRecord


def parse_volunteer_hours(soup: BeautifulSoup) -> list[VolunteerRecord]:
    """Parse /schools/{id}/users/{uid}/volunteer_records/new -> list of VolunteerRecord.

    Volunteer hours structure:
      table > tr#volunteer_record_{id}
        td[0] — month (e.g. "Dec 2025")
        td[1] — activity (e.g. "School Event") + note in <em> (e.g. "robotics")
        td[2] — hours (e.g. "2:30 hrs")
    """
    records: list[VolunteerRecord] = []

    table = soup.find("table")
    if not table:
        return records

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        month = cells[0].get_text(strip=True)

        # Activity name is the main text, note is in <em>
        activity_cell = cells[1]
        note_el = activity_cell.find("em")
        note = note_el.get_text(strip=True) if note_el else ""
        # Remove the note from the activity text
        if note_el:
            note_el.decompose()
        activity = activity_cell.get_text(strip=True)

        hours = cells[2].get_text(strip=True)

        if month or activity:
            records.append(VolunteerRecord(
                month=month,
                activity=activity,
                note=note,
                hours=hours,
            ))

    return records
