from __future__ import annotations

from icalendar import Calendar

from parentsquare_mcp.models import CalendarEvent


def parse_ics_calendar(ics_text: str) -> list[CalendarEvent]:
    """Parse ICS text into a list of CalendarEvent objects."""
    cal = Calendar.from_ical(ics_text)
    events: list[CalendarEvent] = []

    for component in cal.walk("VEVENT"):
        dtstart = component.get("DTSTART")
        dtend = component.get("DTEND")

        # Determine if all-day event (date vs datetime)
        all_day = False
        start_str = ""
        if dtstart:
            dt = dtstart.dt
            if hasattr(dt, "hour"):
                start_str = dt.isoformat()
            else:
                start_str = dt.isoformat()
                all_day = True

        end_str = None
        if dtend:
            end_str = dtend.dt.isoformat()

        title = str(component.get("SUMMARY", "")).strip()
        location = str(component.get("LOCATION", "")).strip() or None
        description = str(component.get("DESCRIPTION", "")).strip() or None

        events.append(
            CalendarEvent(
                title=title,
                start=start_str,
                end=end_str,
                location=location,
                description=description,
                all_day=all_day,
            )
        )

    return events
