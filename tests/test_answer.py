import anthropic
import httpx
import pytest

from ole.answer import SourceNumbering, answer_text, build_messages, rewrite_query
from ole.search import Hit


def hit(url="https://wp.stolaf.edu/x/", lastmod=None, title="Page", heading="Sec"):
    return Hit(id="1", url=url, title=title, heading_path=heading, text="Some text.", lastmod=lastmod, score=1.0)


def test_build_messages_shape():
    hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    msgs = build_messages(hist, "q?", [hit(lastmod="2021-03-01T10:00:00Z")])
    assert msgs[:2] == hist
    last = msgs[-1]
    assert last["role"] == "user"
    sr = last["content"][0]
    assert sr["type"] == "search_result" and sr["citations"] == {"enabled": True}
    assert sr["source"] == "https://wp.stolaf.edu/x/"
    assert sr["content"] == [{"type": "text", "text": "Some text."}]
    assert last["content"][-1] == {"type": "text", "text": "q?"}


def test_search_result_title_has_date():
    title = build_messages([], "q", [hit(lastmod="2021-03-01T10:00:00Z")])[-1]["content"][0]["title"]
    assert title == "Page | Sec (updated 2021-03-01)"
    assert build_messages([], "q", [hit(heading="")])[-1]["content"][0]["title"] == "Page (date unknown)"


def test_no_results_still_valid():
    content = build_messages([], "q", [])[-1]["content"]
    assert all(b["type"] == "text" for b in content) and content[-1]["text"] == "q"


class FakeClient:
    def __init__(self):
        self.calls = 0
        outer = self

        class Messages:
            def create(self, **kw):
                outer.calls += 1
                outer.kw = kw

                class R:
                    content = [type("T", (), {"type": "text", "text": "What are CS major requirements for sophomores?"})()]
                    stop_reason = "end_turn"
                return R()
        self.messages = Messages()


def test_rewrite_uses_history_and_glossary():
    fake = FakeClient()
    hist = [{"role": "user", "content": "CS major requirements?"}, {"role": "assistant", "content": "..."}]
    assert rewrite_query(hist, "what about sophomores?", fake) == "What are CS major requirements for sophomores?"
    assert fake.calls == 1 and fake.kw["model"] == "claude-haiku-5-5"
    prompt = fake.kw["messages"][0]["content"]
    assert "CS major requirements?" in prompt and "what about sophomores?" in prompt
    assert "World Languages and Cultures" in fake.kw["system"]


def test_rewrite_runs_without_history_and_falls_back_on_error():
    fake = FakeClient()
    rewrite_query([], "how many language classes do I need", fake)
    assert fake.calls == 1

    class Broken:
        class messages:
            @staticmethod
            def create(**kw):
                raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    assert rewrite_query([], "original q", Broken()) == "original q"


def test_abbreviations_are_not_sentence_ends():
    from ole.answer import place_cites
    evs = [{"type": "text", "text": "Not during Interim"}, {"type": "cite", "n": 1},
           {"type": "text", "text": ", St. Olaf's January term. Done"}]
    text = "".join(e["text"] if e["type"] == "text" else f"[{e['n']}]" for e in place_cites(iter(evs)))
    assert text == "Not during Interim, St. Olaf's January term.[1] Done"


def test_citation_numbering():
    n = SourceNumbering()
    assert [n.cite("B", "b"), n.cite("A", "a"), n.cite("B", "b")] == [1, 2, 1]
    assert n.items == [{"n": 1, "url": "B", "title": "b"}, {"n": 2, "url": "A", "title": "a"}]


@pytest.mark.live
def test_live_smoke():
    text, sources, ids = answer_text([], "Where is the Piper Center and how do I make an appointment?")
    assert text.strip() and ids
    assert any("stolaf.edu" in s["url"] for s in sources), (text, sources)


def test_cites_move_to_sentence_end():
    from ole.answer import place_cites
    evs = [{"type": "text", "text": "Students get help with"}, {"type": "cite", "n": 2},
           {"type": "text", "text": " the major. Next"}, {"type": "text", "text": " part"}, {"type": "cite", "n": 1}]
    out = list(place_cites(iter(evs)))
    text = "".join(e["text"] if e["type"] == "text" else f"[{e['n']}]" for e in out)
    assert text == "Students get help with the major.[2] Next part[1]"


def test_duplicate_cites_collapse():
    from ole.answer import place_cites
    evs = [{"type": "text", "text": "Email first"}, {"type": "cite", "n": 4}, {"type": "text", "text": " (cc Nate"},
           {"type": "cite", "n": 4}, {"type": "text", "text": ")."}]
    text = "".join(e["text"] if e["type"] == "text" else f"[{e['n']}]" for e in place_cites(iter(evs)))
    assert text == "Email first (cc Nate).[4]"


def test_cite_after_complete_sentence_stays_on_it():
    from ole.answer import place_cites
    evs = [{"type": "text", "text": "The major needs 10 courses."}, {"type": "cite", "n": 1},
           {"type": "text", "text": " You can declare in your second year."}, {"type": "cite", "n": 2},
           {"type": "text", "text": " Done."}]
    text = "".join(e["text"] if e["type"] == "text" else f"[{e['n']}]" for e in place_cites(iter(evs)))
    assert text == "The major needs 10 courses.[1] You can declare in your second year.[2] Done."


def test_infrastructure_errors_become_error_events(monkeypatch):
    import ole.answer as a

    def boom(history, question):
        raise ConnectionError("qdrant down")
    monkeypatch.setattr(a, "retrieve", boom)
    events = list(a.stream_answer([], "q"))
    assert events[-1]["type"] == "error" and "search" in events[-1]["message"].lower()


def test_system_prompt_has_today_and_untrusted_results():
    from datetime import date
    from ole.answer import system_prompt
    p = system_prompt()
    assert date.today().isoformat() in p
    assert "not instructions" in p


def test_unknown_date_is_marked():
    assert build_messages([], "q", [hit(lastmod=None)])[-1]["content"][0]["title"] == "Page | Sec (date unknown)"
