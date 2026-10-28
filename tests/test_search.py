import pytest

from ole.search import Hit, diversify, search


def hit(url, i):
    return Hit(id=f"{url}{i}", url=url, title="t", heading_path="", text="x", lastmod=None, score=1.0 - i / 100)


def test_diversify():
    hits = [hit("A", i) for i in range(5)] + [hit("B", 9)]
    assert [h.url for h in diversify(hits, per_url=3)] == ["A", "A", "A", "B"]


@pytest.mark.integration
def test_course_code_exact():
    urls = [h.url for h in search("CSCI 251", k=3)]
    assert any("catalog.stolaf.edu" in u and "computer-science" in u for u in urls), urls


@pytest.mark.integration
def test_piper_internships():
    urls = [h.url for h in search("how do I find an internship", k=5)]
    assert any("/pipercenter/" in u for u in urls), urls


def test_embed_texts_batches_in_order(monkeypatch):
    import ole.search as s

    calls = []

    class FakeEmb:
        def create(self, model, input, dimensions):
            calls.append((model, len(input), dimensions))
            return type("R", (), {"data": [type("D", (), {"index": i, "embedding": [float(len(t))]})()
                                           for i, t in reversed(list(enumerate(input)))]})()

    monkeypatch.setattr(s, "openai_client", lambda: type("C", (), {"embeddings": FakeEmb()})())
    texts = ["x" * (i % 7 + 1) for i in range(600)]
    out = s.embed_texts(texts)
    assert [v[0] for v in out] == [float(len(t)) for t in texts]
    assert [c[1] for c in calls] == [256, 256, 88]
    assert calls[0][0] == "text-embedding-3-large" and calls[0][2] == 1024


def test_merge_neighbors():
    from ole.search import merge_neighbors
    a = Hit(id="a", url="U", title="t", heading_path="Req", text="middle", lastmod=None, score=1.0, index=5)
    b = Hit(id="b", url="U", title="t", heading_path="Req", text="six", lastmod=None, score=0.9, index=6)
    c = Hit(id="c", url="V", title="t", heading_path="", text="only", lastmod=None, score=0.8, index=0)
    texts = {("U", 4): "before", ("U", 5): "middle", ("U", 6): "six", ("U", 7): "after", ("V", 1): "next"}
    out = merge_neighbors([a, b, c], texts)
    assert out[0].text == "before\n\nmiddle"          # 6 is its own hit, so not glued onto 5
    assert out[1].text == "six\n\nafter"
    assert out[2].text == "only\n\nnext"


def test_chunk_index_stored():
    from ole.chunk import chunk_sections
    from ole.extract import Section
    cs = chunk_sections("U", "T", [Section("H", "w " * 700)], None, "html")
    assert [c.index for c in cs] == [0, 1, 2]
