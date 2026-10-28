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
