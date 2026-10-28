import ole.add_doc as add_doc_mod
from ole.db import connect, save_page


def test_add_doc_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(add_doc_mod, "RAW_DIR", tmp_path / "raw")
    save_page("https://wp.stolaf.edu/a/", "x.html", "html", "crawl", "A", None)
    src = tmp_path / "doc.txt"
    src.write_text("Handbook text")
    url = "https://docs.google.com/document/d/ABC/edit"
    add_doc_mod.add_doc(url, "Handbook", src)
    with connect() as con:
        con.execute("UPDATE pages SET hash='old'")
    src.write_text("Handbook text v2")
    add_doc_mod.add_doc(url, "Handbook", src)
    with connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM pages")]
    mine = [r for r in rows if r["url"] == url]
    assert len(rows) == 2 and len(mine) == 1
    assert mine[0]["kind"] == "text" and mine[0]["source"] == "drive_mcp" and mine[0]["title"] == "Handbook"
    assert mine[0]["hash"] is None  # re-embedded by the next ole.index
    assert (tmp_path / "raw" / mine[0]["file"]).read_text() == "Handbook text v2"
