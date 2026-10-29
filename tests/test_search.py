import json
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


def test_voyage_rerank_scores_in_input_order(monkeypatch):
    import httpx
    import ole.search as s
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"data": [{"index": 1, "relevance_score": 0.9},
                                                  {"index": 0, "relevance_score": 0.2}]})

    monkeypatch.setattr(s, "http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
    assert s.voyage_scores("career coach", ["dining hours", "book a coach"]) == [0.2, 0.9]
    assert seen["body"]["model"] == "rerank-3" and seen["body"]["query"] == "career coach"
    assert seen["body"]["documents"] == ["dining hours", "book a coach"] and seen["auth"] == "Bearer vk-test"


def test_voyage_failure_falls_back_to_local(monkeypatch):
    import httpx
    import ole.search as s
    monkeypatch.setattr(s, "http", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    monkeypatch.setattr(s, "local_scores", lambda q, docs: [1.0] * len(docs))
    assert s.rerank_scores("q", ["a", "b"], "voyage") == [1.0, 1.0]


def test_voyage_retries_rate_limit(monkeypatch):
    import httpx
    import ole.search as s
    codes = iter([429, 429, 200])

    def handler(req):
        c = next(codes)
        return httpx.Response(c, json={"data": [{"index": 0, "relevance_score": 0.5}]} if c == 200 else {})
    monkeypatch.setattr(s, "http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(s.time, "sleep", lambda t: None)
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
    assert s.voyage_scores("q", ["a"]) == [0.5]


def test_fallbacks_are_counted(monkeypatch):
    import httpx
    import ole.search as s
    monkeypatch.setattr(s, "http", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    monkeypatch.setattr(s, "local_scores", lambda q, docs: [1.0] * len(docs))
    before = s.FALLBACKS[0]
    s.rerank_scores("q", ["a"], "voyage")
    assert s.FALLBACKS[0] == before + 1


def test_search_uses_raw_question_too(monkeypatch):
    import numpy as np
    import ole.search as s
    seen = {}
    monkeypatch.setattr(s, "embed_texts", lambda qs: seen.setdefault("embedded", qs) and [[0.0]] * len(qs))
    sparse = type("Sp", (), {"query_embed": lambda self, q: iter([type("V", (), {"indices": np.array([1]), "values": np.array([1.0])})()])})
    monkeypatch.setattr(s, "sparse_model", lambda: sparse())
    point = type("P", (), {"id": "1", "score": 1.0, "payload": {"url": "u", "title": "t", "text": "x", "index": 0}})()

    class Q:
        def query_points(self, *a, prefetch, limit, **kw):
            seen["prefetches"], seen["limit"] = len(prefetch), limit
            return type("R", (), {"points": [point]})()
    monkeypatch.setattr(s, "qdrant", lambda: Q())
    monkeypatch.setattr(s, "rerank_scores", lambda q, docs, which: seen.setdefault("rerank_q", q) and [1.0] * len(docs))
    monkeypatch.setattr(s, "expand", lambda hits, question: hits)
    s.search("j-term interim requirement", raw="do i need a j term every year")
    assert seen["embedded"] == ["j-term interim requirement", "do i need a j term every year"]
    assert seen["prefetches"] == 4 and seen["limit"] == 2 * s.RETRIEVE_K
    assert seen["rerank_q"] == "do i need a j term every year\nj-term interim requirement"


def test_merge_neighbors_adds_chunk_after_next_for_far_hits():
    from ole.search import merge_neighbors
    a = Hit(id="a", url="U", title="t", heading_path="", text="hit", lastmod=None, score=1.0, index=5)
    b = Hit(id="b", url="V", title="t", heading_path="", text="hit", lastmod=None, score=0.9, index=0)
    texts = {("U", 4): "four", ("U", 6): "six", ("U", 7): "seven", ("V", 1): "one", ("V", 2): "two"}
    out = merge_neighbors([a, b], texts, far={("U", 5)})
    assert out[0].text == "four\n\nhit\n\nsix\n\nseven"
    assert out[1].text == "hit\n\none"  # not in far: next chunk only


def test_merge_neighbors_far_stops_at_another_hit():
    from ole.search import merge_neighbors
    a = Hit(id="a", url="U", title="t", heading_path="", text="hit", lastmod=None, score=1.0, index=5)
    b = Hit(id="b", url="U", title="t", heading_path="", text="seven", lastmod=None, score=0.9, index=7)
    out = merge_neighbors([a, b], {("U", 6): "six", ("U", 8): "eight"}, far={("U", 5)})
    assert out[0].text == "hit\n\nsix"


def _fake_neighbors(monkeypatch, scores):
    import ole.search as s
    pts = [type("P", (), {"payload": {"url": "U", "index": i, "heading_path": "Dinner" if i == 7 else "", "text": f"t{i}"}})()
           for i in (4, 6, 7)]
    monkeypatch.setattr(s, "qdrant", lambda: type("Q", (), {"retrieve": lambda self, col, ids, **kw: pts})())
    seen = {}

    def voyage(q, docs):
        seen["q"], seen["docs"] = q, docs
        if scores is None:
            raise s.httpx.ConnectError("down")
        return [scores] * len(docs)
    monkeypatch.setattr(s, "voyage_scores", voyage)
    monkeypatch.setattr(s, "RERANKER", "voyage")
    return s, seen


def test_expand_adds_chunk_after_next_when_relevant(monkeypatch):
    s, seen = _fake_neighbors(monkeypatch, 0.9)
    h = Hit(id="a", url="U", title="Ole Dining", heading_path="Stav Hall", text="t5", lastmod=None, score=1.0, index=5)
    assert s.expand([h], "stav hall hours")[0].text == "t4\n\nt5\n\nt6\n\nt7"
    assert seen["q"] == "stav hall hours" and seen["docs"] == ["Ole Dining\nStav Hall\nDinner\n\nt7"]  # under the hit's headings


def test_expand_skips_chunk_after_next_when_irrelevant_or_voyage_down(monkeypatch):
    for scores in (0.2, None):
        s, _ = _fake_neighbors(monkeypatch, scores)
        h = Hit(id="a", url="U", title="t", heading_path="", text="t5", lastmod=None, score=1.0, index=5)
        assert s.expand([h], "q")[0].text == "t4\n\nt5\n\nt6"
