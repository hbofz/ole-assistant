import json
import os

import pytest

import ole.db as db

A = {"url": "https://wp.stolaf.edu/classics", "file": "a.html", "kind": "html", "lastmod": None, "source": "crawl",
     "title": "Old"}
B = {"url": "https://wp.stolaf.edu/classics/", "file": "b.html", "kind": "html", "lastmod": "2026-01-01",
     "source": "crawl", "title": "Classics"}
C = {"url": "https://docs.google.com/document/d/X/edit", "file": "c.txt", "kind": "text", "lastmod": None,
     "source": "drive_mcp", "title": "Doc"}
D = {"url": "https://wp.stolaf.edu/art", "file": "d.html", "kind": "html", "lastmod": None, "source": "crawl",
     "title": "Art"}


def rows():
    with db.connect() as con:
        return {r["url"]: dict(r) for r in con.execute("SELECT * FROM pages")}


def test_migrate_round_trip_merges_twins(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "b.html").write_text("x")
    os.utime(raw / "b.html", (1_700_000_000, 1_700_000_000))
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("".join(json.dumps(r) + "\n" for r in (A, B, C, D)) + '{"url": "https://wp.stolaf.edu/cut')
    monkeypatch.setattr(db, "RAW_DIR", raw)
    monkeypatch.setattr(db, "MANIFEST", manifest)

    assert db.migrate() == (3, 1, ["https://wp.stolaf.edu/art", "https://wp.stolaf.edu/classics"])
    got = rows()
    assert set(got) == {B["url"], C["url"], "https://wp.stolaf.edu/art/"}
    assert got[B["url"]] == {**B, "hash": None, "status": "live", "fetched_at": "2023-11-14T22:13:20+00:00",
                             "checked_at": "2023-11-14T22:13:20+00:00"}
    assert got[C["url"]]["source"] == "drive_mcp" and got["https://wp.stolaf.edu/art/"]["file"] == "d.html"
    assert not manifest.exists() and (tmp_path / "manifest.jsonl.bak").exists()
    assert db.migrate() == (0, 0, [])  # nothing left to import


def test_save_page_resets_hash_and_mark_gone():
    db.save_page("https://wp.stolaf.edu/a/", "a.html", "html", "crawl", "A", "2026-01-01")
    with db.connect() as con:
        con.execute("UPDATE pages SET hash='h'")
    db.save_page("https://wp.stolaf.edu/a/", "a.html", "html", "crawl", "A2", "2026-02-01")
    r = rows()["https://wp.stolaf.edu/a/"]
    assert r["hash"] is None and r["title"] == "A2" and r["status"] == "live" and r["fetched_at"]
    db.mark_gone("https://wp.stolaf.edu/a/")
    db.mark_gone("https://wp.stolaf.edu/missing/", "2026-03-01")
    got = rows()
    assert got["https://wp.stolaf.edu/a/"]["status"] == "gone" and got["https://wp.stolaf.edu/a/"]["file"] == "a.html"
    assert got["https://wp.stolaf.edu/a/"]["lastmod"] == "2026-02-01"
    # a sitemap entry that 404s keeps its lastmod, so it isn't fetched again until it changes
    assert got["https://wp.stolaf.edu/missing/"]["status"] == "gone"
    assert got["https://wp.stolaf.edu/missing/"]["lastmod"] == "2026-03-01"


def test_lock_is_exclusive():
    with db.lock():
        with pytest.raises(SystemExit) as e:
            with db.lock():
                pass
    assert "crawl.lock" in str(e.value.code)
    with db.lock():  # released when the holder exits
        pass
