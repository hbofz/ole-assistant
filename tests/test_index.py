import hashlib

import pytest
from qdrant_client import models

import ole.index as index_mod
from ole.db import connect, mark_gone, save_page

U, V, W = "https://wp.stolaf.edu/u/", "https://wp.stolaf.edu/v/", "https://wp.stolaf.edu/w/"


def page(tmp_path, url, text):
    name = hashlib.sha1(url.encode()).hexdigest() + ".txt"
    (tmp_path / name).write_text(text)
    save_page(url, name, "text", "gdoc_export", "Doc", None)
    return tmp_path / name


def rows():
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM pages WHERE status='live' ORDER BY url")]


def indices(client, url):
    pts, _ = client.scroll("test", models.Filter(must=[models.FieldCondition(key="url", match=models.MatchValue(value=url))]),
                           limit=100, with_payload=True)
    return sorted(p.payload["index"] for p in pts)


def test_incremental_skips_unchanged_and_leaves_no_orphans(tmp_path, qd):
    client, calls = qd
    u = page(tmp_path, U, "word " * 700)
    page(tmp_path, V, "same text on two pages " * 20)
    page(tmp_path, W, "same text on two pages " * 20)
    assert index_mod.index_pages(rows(), client) == [U, V, W]
    assert indices(client, U) == [0, 1, 2] and indices(client, W) == [0]  # no cross-page dedupe
    assert all(r["hash"] for r in rows())

    n = len(calls)
    assert index_mod.index_pages(rows(), client) == [] and len(calls) == n  # unchanged: no embedding call

    u.write_text("word " * 100)
    assert index_mod.index_pages(rows(), client) == [U]
    assert indices(client, U) == [0] and indices(client, V) == [0]


def test_failed_embedding_never_writes_hash(tmp_path, qd, monkeypatch):
    client, _ = qd
    page(tmp_path, U, "word " * 50)

    def boom(texts):
        raise RuntimeError("openai down")

    monkeypatch.setattr(index_mod, "embed_texts", boom)
    with pytest.raises(RuntimeError):
        index_mod.index_pages(rows(), client)
    assert rows()[0]["hash"] is None


def test_build_index_incremental_then_full(tmp_path, qd):
    client, calls = qd
    page(tmp_path, U, "word " * 50)
    assert index_mod.build_index(client) == 1
    page(tmp_path, V, "other " * 50)
    mark_gone(U)
    index_mod.delete_points(client, [U])
    before = sum(calls)
    assert index_mod.build_index(client) == 1 and sum(calls) - before == 1  # only the new page is embedded
    assert index_mod.build_index(client, full=True) == 1 and sum(calls) - before == 2
    assert indices(client, V) == [0] and indices(client, U) == []
