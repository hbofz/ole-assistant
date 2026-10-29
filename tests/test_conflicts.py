import json

import pytest

import ole.conflicts as cf
import ole.index as index_mod
from ole.db import connect, mark_gone, put

PAY = "Students earn $13.50/hour for up to 40 hours a week."
NEW_PAY = "Students earn $14.50/hour for up to 40 hours a week."


def test_page_blocks_keep_headings(tmp_path, monkeypatch):
    # CURI puts its pay rate in an <h4>, so headings must reach the extractor
    monkeypatch.setattr(index_mod, "RAW_DIR", tmp_path)
    body = "<p>Summer research positions are open to all students in every discipline on campus.</p>" * 3
    (tmp_path / "p.html").write_text(f"<html><head><title>Research – CURI</title></head><body><main><h2>Pay</h2>"
                                     f"<h4>$14.50/hour (projects run up to 40hrs/wk)</h4>{body}</main></body></html>")
    title, blocks = cf.page_blocks({"url": "https://wp.stolaf.edu/curi/r/", "file": "p.html", "kind": "html",
                                    "title": None})
    assert title == "Research – CURI"
    assert any("$14.50/hour" in b for b in blocks)


def test_long_pages_split_at_section_boundaries():
    blocks = ["## A\n" + "w " * 40, "## B\n" + "w " * 40, "## C\n" + "w " * 40]
    parts = cf.parts("Title", blocks, limit=100)
    assert len(parts) == 2 and parts[0].startswith("# Title") and parts[1].startswith("# Title")
    assert "## A" in parts[0] and "## B" in parts[0] and "## C" in parts[1]
    assert cf.parts("Title", blocks[:1], limit=100) == ["# Title\n\n" + blocks[0]]


def test_gate():
    facty = "Pay is $13.50/hour. Applications are due February 16. The internship is 0.5 credit."
    assert cf.gated("https://wp.stolaf.edu/curi/x/", facty)
    assert not cf.gated("https://wp.stolaf.edu/curi/x/", "Welcome to CURI! We love research.")
    assert not cf.gated("https://wp.stolaf.edu/curi/x/", "Contact us at 507-786-3000.")
    assert cf.gated("https://wp.stolaf.edu/x/", "Complete 3 courses and 1 credit.")
    for url in ("https://wp.stolaf.edu/news-archive/x/", "https://wp.stolaf.edu/wp-content/uploads/2019/05/a.pdf",
                "https://www.stolaf.edu/profile/someone/"):
        assert not cf.gated(url, facty)


def fact(key="CURI summer researcher hourly pay", value="$13.50/hour", quote=PAY, as_of=None):
    return {"key": key, "value": value, "quote": quote, "as_of": as_of}


def test_clean_facts_keeps_checkable_verbatim_facts():
    text = f"# CURI\n\n## Pay\n{PAY}\nEmail curi@stolaf.edu with questions. Ask **Jane Doe**."
    kept = cf.clean_facts([
        fact(),
        fact(value="competitive"),  # no digit
        fact(quote="Students earn $99/hour."),  # not on the page
        fact(key="CURI contact email", value="curi@stolaf.edu", quote="Email curi@stolaf.edu with questions."),
        fact(key="CURI program contact", value="Jane Doe", quote="Ask Jane Doe."),  # markdown stripped to match
    ], text)
    assert [f["value"] for f in kept] == ["$13.50/hour", "curi@stolaf.edu", "Jane Doe"]


def pages(monkeypatch, texts: dict[str, str]):
    with connect() as con:
        for url in texts:
            put(con, {"url": url, "file": "x", "kind": "html", "title": url, "lastmod": "2026-01-01"})
    monkeypatch.setattr(cf, "page_blocks", lambda row: (row["url"], [texts[row["url"]]]))


def fake_ask(calls, result=lambda prompt: {"facts": [fact()]}):
    def ask(prompt, system, schema, model="claude-haiku-5-5", effort="low"):
        calls.append({"prompt": prompt, "system": system, "schema": schema, "model": model})
        out = result(prompt)
        if isinstance(out, Exception):
            raise out
        return out
    return ask


def stored_facts():
    with connect() as con:
        cf.ensure(con)
        return [dict(r) for r in con.execute("SELECT url, key, value, quote, as_of FROM facts ORDER BY url, id")]


def test_extract_is_incremental_by_text_hash(monkeypatch):
    texts = {"https://wp.stolaf.edu/curi/a/": f"{PAY} Due February 16.", "https://wp.stolaf.edu/curi/b/": "Hello!"}
    pages(monkeypatch, texts)
    calls = []
    monkeypatch.setattr(cf, "ask", fake_ask(calls))
    assert cf.extract(workers=2)["done"] == 1  # b has no fact-looking tokens
    assert calls[0]["model"] == "claude-haiku-5-5" and "course descriptions" in calls[0]["system"]
    assert PAY in calls[0]["prompt"]
    assert stored_facts() == [{"url": "https://wp.stolaf.edu/curi/a/", **fact()}]

    assert cf.extract()["done"] == 0 and len(calls) == 1  # unchanged text: no call

    texts["https://wp.stolaf.edu/curi/a/"] = f"{NEW_PAY} Due February 16."
    monkeypatch.setattr(cf, "ask", fake_ask(calls, lambda p: {"facts": [fact(value="$14.50/hour", quote=NEW_PAY)]}))
    cf.extract()
    assert [f["value"] for f in stored_facts()] == ["$14.50/hour"]  # replaced, not appended


def test_page_with_no_facts_is_not_re_extracted_but_a_failed_one_is(monkeypatch):
    texts = {"https://wp.stolaf.edu/a/": f"{PAY} 3 credits.", "https://wp.stolaf.edu/b/": f"{NEW_PAY} 4 credits."}
    pages(monkeypatch, texts)
    calls = []
    monkeypatch.setattr(cf, "ask", fake_ask(calls, lambda p: {"facts": []} if "13.50" in p else RuntimeError("down")))
    assert cf.extract() == {"pages": 2, "done": 1, "failed": 1}
    monkeypatch.setattr(cf, "ask", fake_ask(calls := []))
    assert cf.extract()["done"] == 1 and len(calls) == 1 and NEW_PAY in calls[0]["prompt"]


def test_page_that_falls_out_of_the_gate_loses_its_facts(monkeypatch):
    texts = {"https://wp.stolaf.edu/a/": f"{PAY} 3 credits."}
    pages(monkeypatch, texts)
    monkeypatch.setattr(cf, "ask", fake_ask([]))
    cf.extract()
    texts["https://wp.stolaf.edu/a/"] = "We moved this page."
    cf.extract()
    assert stored_facts() == []


def test_norm():
    assert cf.norm("$14.50/hour") == cf.norm("14.50 per hour") == cf.norm(" $14.50 / Hour")
    assert cf.norm("$1,000") == cf.norm("1000")
    assert cf.norm("$13.50/hour") != cf.norm("$14.50/hour")
    assert cf.norm("0-3 courses") != cf.norm("1-3 courses")
    assert cf.norm("0-3.00 credits") == cf.norm("0-3 courses")  # one course is one credit
    assert cf.norm("1.0 credit") == cf.norm("1 course") != cf.norm("0.5 credit")


def F(id, url, value, key="CURI summer researcher hourly pay", quote=None):
    return {"id": id, "url": url, "key": key, "value": value, "quote": quote or f"Pay is {value}.", "as_of": None}


def test_groups_join_near_identical_keys_and_keep_only_differing_values():
    import numpy as np
    facts = [F(1, "a", "$13.50/hour"), F(2, "b", "$14.50 per hour"), F(3, "c", "$14.50/hour"),
             F(4, "d", "$12/hour", key="student employment NST1 pay"),
             F(5, "e", "3 credits", key="x"), F(6, "f", "3 credits", key="x")]
    v = np.array([[1, 0, 0], [0.99, 0.14, 0], [1, 0, 0], [0.6, 0.8, 0], [0, 0, 1], [0, 0, 1]], dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    gs = cf.groups(facts, v, threshold=0.9)
    assert [[f["id"] for f in g] for g in gs] == [[1, 2, 3]]  # NST1 too far; 5 and 6 agree


def setup_find(monkeypatch, facts, lastmods=None):
    """Live pages for the facts' URLs, the facts themselves, and fake embeddings: one direction per key."""
    keys = sorted({f["key"] for f in facts})
    with connect() as con:
        cf.ensure(con)
        for url in sorted({f["url"] for f in facts}):
            put(con, {"url": url, "title": "T " + url.rsplit("/", 2)[-2], "lastmod": (lastmods or {}).get(url)})
        con.executemany("INSERT INTO facts (id, url, text_hash, key, value, quote, as_of) VALUES (?, ?, 'h', ?, ?, ?, ?)",
                        [(f["id"], f["url"], f["key"], f["value"], f["quote"], f["as_of"]) for f in facts])
    embedded = []

    def embed(texts):
        embedded.extend(texts)
        return [[float(keys.index(t) == i) for i in range(len(keys))] for t in texts]
    monkeypatch.setattr(cf, "embed_texts", embed)
    return embedded


A, B = "https://wp.stolaf.edu/curi/student-applications-2/", "https://wp.stolaf.edu/curi/research-programs/"


def conflicts():
    with connect() as con:
        cf.ensure(con)
        return [dict(r) for r in con.execute("SELECT * FROM conflicts")]


def test_find_judges_new_groups_once_and_prunes_gone_pages(monkeypatch):
    embedded = setup_find(monkeypatch, [F(1, A, "$13.50/hour"), F(2, B, "$14.50/hour")],
                          {A: "2023-06-07T13:15:15Z", B: "2026-10-16T15:26:10Z"})
    calls = []
    monkeypatch.setattr(cf, "ask", fake_ask(calls, lambda p: {"verdict": "resolved", "current_url": B,
                                                              "reason": "B is newer."}))
    assert cf.find(threshold=0.9) == {"groups": 1, "judged": 1, "failed": 0}
    assert calls[0]["model"] == "claude-sonnet-5-5"
    assert A in calls[0]["prompt"] and "$13.50/hour" in calls[0]["prompt"] and "2026-10-16" in calls[0]["prompt"]
    [c] = conflicts()
    assert (c["verdict"], c["current_url"], c["key"]) == ("resolved", B, "CURI summer researcher hourly pay")
    assert sorted(json.loads(c["fact_ids"])) == [1, 2]

    assert cf.find(threshold=0.9)["judged"] == 0 and len(calls) == 1  # same group: no new judgment
    assert len(embedded) == 2  # vectors are stored, not re-embedded

    mark_gone(A)
    cf.find(threshold=0.9)
    assert conflicts() == [] and [f["url"] for f in stored_facts()] == [B]


def test_judge_sees_migration_stamps_and_unknown_dates(monkeypatch):
    C = "https://wp.stolaf.edu/english/httpwp-stolaf-edupipercenter-pay/"
    setup_find(monkeypatch, [F(1, A, "$13.50/hour"), F(2, B, "$14.50/hour"), F(3, C, "$14/hour")],
               {A: "2023-06-07T13:15:15Z", C: "2026-01-01"})
    with connect() as con:
        put(con, {"url": "https://wp.stolaf.edu/pipercenter/"})
        for i in range(101):
            put(con, {"url": f"https://wp.stolaf.edu/m{i}/", "lastmod": "2023-06-07T00:00:00Z"})
    calls = []
    monkeypatch.setattr(cf, "ask", fake_ask(calls, lambda p: {"verdict": "unresolved", "current_url": None,
                                                              "reason": "No signal."}))
    cf.find(threshold=0.9)
    p = calls[0]["prompt"]
    assert "2023-06-07 (shared by 102 live pages)" in p and "unknown" in p
    assert "looks like a copy of: pipercenter" in p
    assert "100" in calls[0]["system"]


@pytest.mark.parametrize("answer,expected", [
    ({"verdict": "resolved", "current_url": "https://elsewhere/", "reason": "r"}, ("unresolved", None)),
    ({"verdict": "not_a_conflict", "current_url": B, "reason": "r"}, ("not_a_conflict", None)),
])
def test_judge_answers_are_validated(monkeypatch, answer, expected):
    setup_find(monkeypatch, [F(1, A, "$13.50/hour"), F(2, B, "$14.50/hour")])
    monkeypatch.setattr(cf, "ask", fake_ask([], lambda p: answer))
    cf.find(threshold=0.9)
    assert [(c["verdict"], c["current_url"]) for c in conflicts()] == [expected]


def test_a_page_contradicting_itself_is_unresolved(monkeypatch):
    setup_find(monkeypatch, [F(1, A, "$13.50/hour"), F(2, A, "$14.50/hour")])
    calls = []
    monkeypatch.setattr(cf, "ask", fake_ask(calls, lambda p: {"verdict": "resolved", "current_url": A, "reason": "r"}))
    cf.find(threshold=0.9)
    assert [c["verdict"] for c in conflicts()] == ["unresolved"]
    assert "same page" in calls[0]["prompt"]


def test_failed_judgment_is_retried_next_run(monkeypatch):
    setup_find(monkeypatch, [F(1, A, "$13.50/hour"), F(2, B, "$14.50/hour")])
    monkeypatch.setattr(cf, "ask", fake_ask([], lambda p: RuntimeError("limit")))
    assert cf.find(threshold=0.9)["failed"] == 1 and conflicts() == []
    monkeypatch.setattr(cf, "ask", fake_ask([], lambda p: {"verdict": "unresolved", "current_url": None, "reason": "r"}))
    assert cf.find(threshold=0.9)["judged"] == 1


def add_conflict(monkeypatch, verdict, current=None):
    setup_find(monkeypatch, [F(1, A, "$13.50/hour", quote=PAY), F(2, B, "$14.50/hour", quote=NEW_PAY)])
    monkeypatch.setattr(cf, "ask", fake_ask([], lambda p: {"verdict": verdict, "current_url": current,
                                                           "reason": "B is newer & says so."}))
    cf.find(threshold=0.9)


def test_notes_for_the_outdated_side(monkeypatch):
    add_conflict(monkeypatch, "resolved", B)
    tags, blocks = cf.notes([(A, f"Intro. {PAY} More.")])
    assert tags == {A: " (outdated on CURI summer researcher hourly pay; see newer page)"}
    [b] = blocks
    assert b["type"] == "search_result" and b["source"] == B and b["title"] == "T research-programs"
    assert NEW_PAY in b["content"][0]["text"] and "T student-applications-2" in b["content"][0]["text"]
    assert cf.notes([(B, NEW_PAY)]) == ({}, [])  # the current side needs no note
    assert cf.notes([(A, "A chunk of the same page about housing.")]) == ({}, [])  # not the part that conflicts


def test_notes_for_unresolved(monkeypatch):
    add_conflict(monkeypatch, "unresolved")
    tags, blocks = cf.notes([(A, PAY), (B, "x " + NEW_PAY)])
    assert tags == {}
    assert blocks == [{"type": "text", "text": "St. Olaf's pages disagree on CURI summer researcher hourly pay: "
                       "T student-applications-2 says $13.50/hour, T research-programs says $14.50/hour. "
                       "Tell the student both and which office to ask."}]


def test_notes_without_any_conflicts_tables():
    assert cf.notes([("https://wp.stolaf.edu/x/", "text")]) == ({}, [])


def test_report_lists_conflicts_by_office(monkeypatch, tmp_path):
    add_conflict(monkeypatch, "resolved", B)
    out = tmp_path / "conflicts.html"
    cf.report(out)
    html = out.read_text()
    assert "<title>" in html and "curi" in html and f'href="{B}"' in html
    assert "B is newer &amp; says so." in html and "$13.50/hour" in html and "outdated" in html.lower()
    assert "1 resolved" in html and "0 unresolved" in html


def test_oversized_groups_split_at_a_tighter_threshold():
    import numpy as np
    # a chain a-b-c-d (links at 0.98, 0.91, 0.98): transitively one group, though a and d are far apart
    angles = np.radians([0, 10, 34, 44])
    v = np.stack([np.cos(angles), np.sin(angles)], axis=1).astype(np.float32)
    facts = [F(i, f"u{i}", f"{i} credits") for i in range(4)]
    assert [len(g) for g in cf.groups(facts, v, threshold=0.9)] == [4]
    assert [[f["id"] for f in g] for g in cf.groups(facts, v, threshold=0.9, max_size=2)] == [[0, 1], [2, 3]]


def test_one_page_groups_need_near_identical_keys():
    import numpy as np
    # one catalog page listing different courses' credits: similar keys, not a contradiction
    v = np.array([[1, 0], [0.9, 0.436], [1, 0], [0.999, 0.045]], dtype=np.float32)
    facts = [F(1, "p", "0.25 credit", key="MUSIC 166 credit"), F(2, "p", "0.5 credit", key="MUSIC 167 credit"),
             F(3, "q", "0-3 courses"), F(4, "q", "1-3 courses")]
    assert cf.groups(facts[:2], v[:2], threshold=0.85) == []
    assert [[f["id"] for f in g] for g in cf.groups(facts[2:], v[2:], threshold=0.85)] == [[3, 4]]


def test_keys_use_st_olafs_name_for_january_term():
    text = "0.25-credit internships are not an option during J-Term."
    [f] = cf.clean_facts([fact(key="Piper Center J-Term 0.25-credit internship option", value="0.25 not allowed",
                               quote=text)], text)
    assert f["key"] == "Piper Center Interim 0.25-credit internship option"
    assert cf.canon_key("J Term and January term and Jterm and j-term") == "Interim and Interim and Interim and Interim"


def test_only_the_facts_the_judge_names_are_in_the_conflict_and_survive_re_extraction(monkeypatch):
    C = "https://wp.stolaf.edu/curi/fall/"
    setup_find(monkeypatch, [F(1, A, "$13.50/hour"), F(2, B, "$14.50/hour"), F(3, C, "$13/hour")])
    calls = []
    monkeypatch.setattr(cf, "ask", fake_ask(calls, lambda p: {"verdict": "resolved", "current_url": B,
                                                              "conflicting": [1, 2], "reason": "r"}))
    cf.find(threshold=0.9)
    assert json.loads(conflicts()[0]["fact_ids"]) == [1, 2]
    assert "conflicting" in calls[0]["schema"]["properties"]
    with connect() as con:  # page A re-extracted with the same facts: new row ids
        con.execute("UPDATE facts SET id=11 WHERE id=1")
    assert cf.find(threshold=0.9)["judged"] == 0
    assert json.loads(conflicts()[0]["fact_ids"]) == [2, 11]
    assert cf.notes([(C, "Pay is $13/hour.")]) == ({}, [])  # the fall rate was never part of it
