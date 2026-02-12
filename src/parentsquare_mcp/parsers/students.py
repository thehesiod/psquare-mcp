from __future__ import annotations

from bs4 import BeautifulSoup

from parentsquare_mcp.models import StudentDashboard


def parse_student_dashboard(soup: BeautifulSoup) -> StudentDashboard:
    """Parse /students/{id}/dashboard -> StudentDashboard.

    Structure:
      .student-info-name-container h3 a  (student name)
      .student-info-name-container > div  (grade)
      .site-header  (school context)
      #student-classes table tbody tr td  (classes)
        div.bold  (class name)
        div > a  (primary teacher)
        div.other-teacher > a  (additional teachers)
    """
    # Student name
    name_container = soup.find("div", class_="student-info-name-container")
    student_name = ""
    grade = None
    if name_container:
        h3 = name_container.find("h3")
        if h3:
            student_name = h3.get_text(strip=True)
        # Grade is in a sibling div after the h3
        grade_div = name_container.find("div")
        if grade_div and grade_div != h3:
            grade = grade_div.get_text(strip=True) or None

    # School name — from sidebar or header
    school_name = ""
    sidebar_selected = soup.find("li", class_="selected-section")
    if sidebar_selected:
        truncate = sidebar_selected.find("div", class_="truncate-text")
        if truncate:
            text = truncate.get_text(strip=True)
            # Format: "Grade • School Name"
            if "•" in text:
                parts = text.split("•")
                school_name = parts[-1].strip()
                if not grade:
                    grade = parts[0].strip() or None
            else:
                school_name = text

    if not school_name:
        header = soup.find("div", class_="site-header")
        if header:
            h2 = header.find("h2")
            if h2:
                school_name = h2.get_text(strip=True)

    # Classes and teachers
    teachers: list[str] = []
    classes: list[str] = []

    classes_box = soup.find("div", id="student-classes")
    if classes_box:
        table = classes_box.find("table")
        if table:
            for row in table.find_all("tr"):
                td = row.find("td")
                if not td:
                    continue

                # Class name
                class_name_div = td.find("div", class_="bold")
                if class_name_div:
                    class_name = class_name_div.get_text(strip=True)
                    classes.append(class_name)

                # Teachers — all links that are user profile links
                for a_tag in td.find_all("a", href=True):
                    href = a_tag["href"]
                    if "/users/" in href and "chat" not in href:
                        teacher_name = a_tag.get_text(strip=True)
                        if teacher_name and teacher_name not in teachers:
                            teachers.append(teacher_name)

    return StudentDashboard(
        student_name=student_name,
        school_name=school_name,
        grade=grade,
        teachers=teachers,
        classes=classes,
    )
