"""The fixture scrubber must never let a live Google document link through.

Google Docs, Drive and Forms ids open a school's real documents (a sign-up
sheet can be editable by anyone who has the link), and the repository is
public, so every id is replaced by a stand-in of the same shape.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("scrub", Path(__file__).parent / "fixtures" / "scrub.py")
scrub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrub)

DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz01-3456_"  # made up


def _id_after(url: str, route: str) -> str:
    return url.split(route, 1)[1].split("/", 1)[0].split("?", 1)[0]


def test_google_document_ids_are_replaced_and_routes_kept():
    for url, route in (
        (f"https://docs.google.com/forms/d/e/{DOC_ID}/viewform?usp=sf_link", "/forms/d/e/"),
        (f"https://docs.google.com/spreadsheets/d/{DOC_ID}/edit?usp=sharing", "/spreadsheets/d/"),
        (f"https://drive.google.com/file/d/{DOC_ID}/view?usp=drive_link", "/file/d/"),
    ):
        out = scrub.scrub_url(url)
        assert DOC_ID not in out
        fake = _id_after(out, route)
        assert len(fake) == len(DOC_ID)
        assert [c.isdigit() for c in fake] == [c.isdigit() for c in DOC_ID]
        assert out.split(route, 1)[1].split("?", 1)[0].endswith(("/viewform", "/edit", "/view"))


def test_one_id_gets_one_stand_in_across_links():
    a = scrub.scrub_url(f"https://docs.google.com/forms/d/e/{DOC_ID}/viewform")
    b = scrub.scrub_url(f"https://drive.google.com/file/d/{DOC_ID}/view")
    assert _id_after(a, "/forms/d/e/") == _id_after(b, "/file/d/")


def test_calendar_links_keep_their_route_and_dates():
    out = scrub.scrub_url(
        "https://www.google.com/calendar/event?action=TEMPLATE"
        "&dates=20260910T100000Z/20260910T110000Z&text=Picnic"
    )
    assert out.startswith("https://www.google.com/calendar/event?")
    assert "dates=20260910T100000Z" in out
