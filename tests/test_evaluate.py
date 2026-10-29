from ole.evaluate import cache_key, summarize


def r(answerable, hit=True, correct=True, declined=False):
    return {"answerable": answerable, "retrieval_hit": hit, "correct": correct, "declined": declined}


def test_summarize_pass():
    rows = [r(True)] * 9 + [r(True, hit=False, correct=False)] + [r(False, hit=None, correct=None, declined=True)] * 2
    s = summarize(rows)
    assert s["retrieval_hit"] == 0.9 and s["correct"] == 0.9 and s["confident_wrong"] == 0
    assert s["passed"] is True


def test_summarize_confident_wrong_fails():
    rows = [r(True)] * 10 + [r(False, hit=None, correct=None, declined=False)]
    s = summarize(rows)
    assert s["confident_wrong"] == 1 and s["passed"] is False


def test_summarize_bars():
    assert summarize([r(True)] * 8 + [r(True, hit=False)] * 2)["passed"] is False  # retrieval 0.8
    assert summarize([r(True)] * 8 + [r(True, correct=False)] * 2)["passed"] is False  # correct 0.8


def test_summarize_only_unanswerable():
    s = summarize([r(False, hit=None, correct=None, declined=True)])
    assert s["retrieval_hit"] is None and s["correct"] is None and s["passed"] is True


def test_cache_key_changes():
    base = cache_key("m", "q", [], ["a", "b"])
    assert base == cache_key("m", "q", [], ["a", "b"])
    assert base != cache_key("m2", "q", [], ["a", "b"])
    assert base != cache_key("m", "q", [], ["a", "c"])


def test_grade_runs_on_the_subscription(monkeypatch):
    import ole.evaluate as ev
    seen = {}

    def ask(prompt, system, schema, model, effort="low"):
        seen.update(prompt=prompt, system=system, schema=schema, model=model)
        return {"correct": True, "declined": False, "reason": "matches"}
    monkeypatch.setattr(ev, "ask", ask)
    row = {"question": "pay?", "expected_answer": "$14.50/hour", "answerable": True}
    assert ev.grade(row, "It pays $14.50/hour.", votes=1) == {"correct": True, "declined": False, "reason": "matches", "votes": 1}
    assert seen["model"] == "claude-sonnet-5-5" and seen["schema"] == ev.GRADE_SCHEMA
    assert seen["system"] == ev.GRADER_PROMPT and "$14.50/hour" in seen["prompt"] and "It pays" in seen["prompt"]


def test_cache_key_rep_gives_fresh_answers():
    assert cache_key("m", "q", [], ["a"]) == cache_key("m", "q", [], ["a"], rep=0)  # old caches still hit
    assert cache_key("m", "q", [], ["a"], rep=1) != cache_key("m", "q", [], ["a"])


def test_run_retrieval_only_skips_answers(monkeypatch, tmp_path):
    import json
    import ole.evaluate as ev
    qs = tmp_path / "q.jsonl"
    qs.write_text(json.dumps({"id": "x1", "area": "a", "question": "q?", "expected_answer": "e",
                              "expected_urls": ["https://a/"], "answerable": True}) + "\n" +
                  json.dumps({"id": "x2", "area": "u", "question": "u?", "expected_answer": "",
                              "expected_urls": [], "answerable": False}) + "\n")
    hit = type("H", (), {"id": "1", "url": "https://a"})()
    monkeypatch.setattr(ev, "retrieve", lambda h, q: [hit])
    monkeypatch.setattr(ev, "answer_text", lambda *a: (_ for _ in ()).throw(AssertionError("answered")))
    out = ev.run("m", questions=qs, retrieval_only=True)
    assert out["summary"]["retrieval_hit"] == 1.0 and out["results"][0]["rank"] == 1


def test_grade_takes_majority_of_votes(monkeypatch):
    import ole.evaluate as ev
    votes = iter([{"reason": "a", "correct": True, "declined": False},
                  {"reason": "b", "correct": False, "declined": True},
                  {"reason": "c", "correct": True, "declined": False}])
    monkeypatch.setattr(ev, "ask", lambda *a, **k: next(votes))
    g = ev.grade({"question": "q", "expected_answer": "e", "answerable": True}, "ans", votes=3)
    assert g["correct"] is True and g["declined"] is False and g["votes"] == 2 and g["reason"] == "a"


def test_grader_reasons_before_verdict():
    import ole.evaluate as ev
    assert list(ev.GRADE_SCHEMA["properties"])[0] == "reason"


def test_compare_pairs_items_across_reps(monkeypatch, tmp_path):
    import json
    import ole.evaluate as ev
    monkeypatch.setattr(ev, "RUNS", tmp_path)

    def run(tag, rep, correct):
        res = [{"id": f"x{i}", "correct": c} for i, c in enumerate(correct)]
        (tmp_path / f"t-m-heldout-{tag}-rep{rep}.json").write_text(json.dumps({"summary": {"correct": 0}, "results": res}))
    run("old", 0, [True, False, False, True]); run("old", 1, [True, False, True, True])
    run("new", 0, [True, True, True, True])
    c = ev.compare("heldout", "old", "new")
    assert c["a"] == 0.625 and c["b"] == 1.0 and c["diff"] == 0.375
    assert c["better"] == ["x1", "x2"] and c["worse"] == [] and c["ci"][0] <= c["diff"] <= c["ci"][1]


def test_answer_cache_sees_hit_text(monkeypatch, tmp_path):
    # the same hit can carry different text (a neighbor chunk glued on or not), so the cache must not reuse its answer
    import json
    import ole.evaluate as ev
    monkeypatch.setattr(ev, "CACHE", tmp_path)
    monkeypatch.setattr(ev, "grade", lambda row, reply, votes: {"correct": True, "declined": False, "reason": "", "votes": 3})
    row = {"id": "x", "question": "q?", "expected_answer": "e", "expected_urls": ["u"], "answerable": True}
    texts, answered = iter(["dinner 4:30-8", "dinner 4:30-8\n\nlunch 10:30-2"]), []
    monkeypatch.setattr(ev, "retrieve", lambda h, q: [type("H", (), {"id": "1", "url": "u", "text": next(texts)})()])
    monkeypatch.setattr(ev, "answer_text", lambda h, q, m, hits: answered.append(hits[0].text) or ("a", [], ["1"]))
    ev.score(row, "m"); ev.score(row, "m")
    assert len(answered) == 2
