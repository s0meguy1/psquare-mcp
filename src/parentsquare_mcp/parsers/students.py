from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from parentsquare_mcp.models import StudentDashboard, StudentSection, Teacher
from parentsquare_mcp.parsers.text import element_text, one_line

_STUDENT_HREF_RE = re.compile(r"/students/(\d+)/dashboard")
_TEACHER_HREF_RE = re.compile(r"/schools/(\d+)/users/(\d+)")


def _school_id_from_gon(soup: BeautifulSoup) -> int | None:
    for script in soup.find_all("script"):
        text = script.string or ""
        if "gon.institute_id" not in text:
            continue
        m = re.search(r"gon\.institute_id\s*=\s*(\d+)", text)
        kind = re.search(r"""gon\.institute_type\s*=\s*["']?(\w+)""", text)
        if m and (kind is None or kind.group(1).lower() == "school"):
            return int(m.group(1))
    return None


def _section_teachers(cell: Tag) -> list[Teacher]:
    teachers: list[Teacher] = []
    by_id: dict[int, Teacher] = {}
    for a_tag in cell.find_all("a"):
        href = a_tag.get("href", "")
        user_id = None
        name = ""
        m = _TEACHER_HREF_RE.search(href)
        if m:
            user_id = int(m.group(2))
            name = one_line(element_text(a_tag))
        elif a_tag.get("data-user") and str(a_tag["data-user"]).isdigit():
            # The "message this teacher" button: data-user / data-user-name
            user_id = int(a_tag["data-user"])
            name = a_tag.get("data-user-name", "")
        elif "/users/" in href and "chat" not in href:
            name = one_line(element_text(a_tag))
        if not name:
            continue
        existing = by_id.get(user_id) if user_id is not None else None
        if existing is None and any(t.name == name for t in teachers):
            continue
        if existing is not None:
            existing.name = existing.name or name
            continue
        teacher = Teacher(name=name, user_id=user_id)
        teachers.append(teacher)
        if user_id is not None:
            by_id[user_id] = teacher
    return teachers


def parse_student_dashboard(soup: BeautifulSoup) -> StudentDashboard:
    """Parse /students/{id}/dashboard -> StudentDashboard.

    Structure (live, 2026-09):
      .student-info-name-container > h1 > a[href=/students/{id}/dashboard]  (name)
      .student-info-name-container > div                                    (grade)
      #student-classes ul.sections-list > li.sections-list-item
        .sections-list-info > div.bold                                      (class)
        a[href=/schools/{school_id}/users/{user_id}]                        (teacher)
        a[data-user][data-user-name]                                        (message-teacher button)
      gon.institute_id / gon.institute_type="School"                        (the student's school)

    The older layout (an ``h3`` name and a ``#student-classes`` table of
    ``div.bold`` + teacher links) still parses.
    """
    student_name = ""
    grade = None
    student_id = None
    name_container = soup.find("div", class_="student-info-name-container")
    if name_container:
        heading = name_container.find(re.compile(r"^h[1-6]$"))
        if heading:
            student_name = one_line(element_text(heading))
            link = heading.find("a", href=_STUDENT_HREF_RE)
            if link:
                student_id = int(_STUDENT_HREF_RE.search(link["href"]).group(1))
        for div in name_container.find_all("div", recursive=False):
            text = one_line(element_text(div))
            if text:
                grade = text
                break

    # School name — from sidebar or header
    school_name = ""
    sidebar_selected = soup.find("li", class_="selected-section")
    if sidebar_selected:
        truncate = sidebar_selected.find("div", class_="truncate-text")
        if truncate:
            text = one_line(element_text(truncate))
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
                school_name = one_line(element_text(h2))

    # Classes and teachers
    sections: list[StudentSection] = []
    classes_box = soup.find("div", id="student-classes") or soup
    rows: list[Tag] = classes_box.select("ul.sections-list > li") or []
    if not rows:
        table = classes_box.find("table") if classes_box is not soup else None
        rows = [td for tr in (table.find_all("tr") if table else []) if (td := tr.find("td"))]
    for row in rows:
        name_div = row.find("div", class_="bold")
        name = one_line(element_text(name_div)) if name_div else ""
        teachers = _section_teachers(row)
        if name or teachers:
            sections.append(StudentSection(name=name, teachers=teachers))

    school_id = _school_id_from_gon(soup)
    if school_id is None:
        ids = {int(m.group(1)) for a in classes_box.find_all("a", href=_TEACHER_HREF_RE)
               if (m := _TEACHER_HREF_RE.search(a["href"]))}
        if len(ids) == 1:
            school_id = ids.pop()

    teachers_flat: list[str] = []
    for section in sections:
        for teacher in section.teachers:
            if teacher.name not in teachers_flat:
                teachers_flat.append(teacher.name)

    return StudentDashboard(
        student_name=student_name,
        school_name=school_name,
        grade=grade,
        teachers=teachers_flat,
        classes=[s.name for s in sections if s.name],
        student_id=student_id,
        school_id=school_id,
        sections=sections,
    )
