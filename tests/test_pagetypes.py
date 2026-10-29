from datetime import date

from ole import pagetypes
from ole.db import connect, put
from ole.search import Hit, type_penalty

FALL = date(2026, 10, 29)


def test_label_stores_kinds_and_skips_unchanged(monkeypatch):
    with connect() as con:
        for u in ("A", "B"):
            put(con, {"url": u, "file": "x", "kind": "html", "title": u, "hash": "h1"})
    monkeypatch.setattr(pagetypes, "card", lambda row: f"Title: {row['title']}")
    calls = []

    def fake_ask(prompt, system, schema, model):
        calls.append(prompt)
        return {"pages": [{"i": 0, "kind": "occasion", "about": "Reunion 2026"}, {"i": 1, "kind": "regular", "about": ""}]}
    monkeypatch.setattr(pagetypes, "ask", fake_ask)
    assert pagetypes.run() == 2
    assert pagetypes.kinds(["A", "B", "C"]) == {"A": ("occasion", "Reunion 2026"), "B": ("regular", "")}
    assert pagetypes.run() == 0 and len(calls) == 1  # nothing changed since


def test_page_kind_checks_the_year():
    k = pagetypes.page_kind
    assert k("past", "2024-25", FALL) == ("past", "2024-25")
    assert k("past", "archived", FALL) == ("past", "archived")
    assert k("past", "Fall 2026", FALL) == ("regular", "")  # this academic year is not past
    assert k("past", "2026-27", date(2027, 3, 1)) == ("regular", "")
    assert k("occasion", "Reunion 2026", FALL) == ("occasion", "Reunion 2026")
    assert k("regular", "", FALL) == ("regular", "")


def page(url):
    return Hit(id=url, url=url, title="t", heading_path="", text="x", lastmod=None, score=0.0)


def test_type_penalty_demotes_occasion_and_past_pages_unless_asked_about():
    kinds = {"reunion": ("occasion", "Reunion 2026"), "breaks": ("occasion", "Academic breaks"),
             "cal": ("past", "2024-25"), "eat": ("regular", "")}
    stav = ["stav hall hours 2026-27", "what time does stav hall close tonight"]
    assert type_penalty(page("reunion"), ["stav hall menu", "what's on the stav hall menu"], kinds, FALL) == 0  # not about dates or hours
    pen = lambda url, qs: type_penalty(page(url), qs, kinds, FALL)
    assert pen("reunion", stav) > 0 and pen("breaks", stav) > 0 and pen("cal", stav) > 0
    assert pen("eat", stav) == 0 and pen("unknown", stav) == 0
    assert pen("reunion", ["reunion weekend contact 2026-27", "who do i contact about reunion weekend"]) == 0  # names it
    assert pen("breaks", ["rolvaag library hours 2026-27", "is rolvaag library open over thanksgiving"]) == 0  # an occasion
    assert pen("cal", ["commencement 2026 date 2026-27", "when was commencement in 2026"]) == 0  # the student named a year
    assert pen("cal", ["piper center events 2026-27", "what piper center events happened last year"]) == 0


def test_new_academic_year_relabels_every_page(monkeypatch):
    # "the 2026-27 calendar" is regular now but past next August, even though the page itself never changed
    with connect() as con:
        put(con, {"url": "A", "file": "x", "kind": "html", "title": "A", "hash": "h1"})
    monkeypatch.setattr(pagetypes, "card", lambda row: "Title: A")
    monkeypatch.setattr(pagetypes, "ask", lambda *a, **k: {"pages": [{"i": 0, "kind": "regular", "about": ""}]})
    year = ["2026-27"]
    monkeypatch.setattr(pagetypes, "year_tag", lambda today=None: year[0])
    assert pagetypes.run() == 1 and pagetypes.run() == 0
    year[0] = "2027-28"
    assert pagetypes.run() == 1
