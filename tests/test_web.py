import json

from fastapi.testclient import TestClient

import ole.web as web

EVENTS = [{"type": "text", "text": "Hi"}, {"type": "cite", "n": 1},
          {"type": "sources", "items": [{"n": 1, "url": "https://wp.stolaf.edu/x/", "title": "X"}]}, {"type": "done"}]


def test_chat_streams_events(monkeypatch):
    seen = {}

    def fake(history, question):
        seen["args"] = (history, question)
        yield from EVENTS

    monkeypatch.setattr(web, "stream_answer", fake)
    r = TestClient(web.app).post("/api/chat", json={"history": [{"role": "user", "content": "a"},
                                                                {"role": "assistant", "content": "b"}],
                                                    "question": " Where? "})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(l[6:]) for l in r.text.split("\n\n") if l.startswith("data: ")]
    assert events == EVENTS
    assert seen["args"][1] == "Where?" and len(seen["args"][0]) == 2


def test_chat_rejects_bad_input():
    c = TestClient(web.app)
    assert c.post("/api/chat", json={"question": "  "}).status_code == 400
    assert c.post("/api/chat", json={"question": "x" * 2001}).status_code == 400
    assert c.post("/api/chat", json={"question": "ok", "history": [{"role": "system", "content": "x"}]}).status_code == 422


def test_index_page():
    r = TestClient(web.app).get("/")
    assert r.status_code == 200 and "Lefse" in r.text


def test_models_warm_on_startup(monkeypatch):
    import threading
    called = threading.Event()
    monkeypatch.setattr(web, "warmup", called.set)
    with TestClient(web.app):
        assert called.wait(2)


def test_status_reports_last_check_of_live_pages():
    from ole.db import connect, put
    with connect() as con:
        put(con, {"url": "https://wp.stolaf.edu/a/", "checked_at": "2026-10-27T03:00:00+00:00"})
        put(con, {"url": "https://wp.stolaf.edu/b/", "checked_at": "2026-10-26T03:00:00+00:00"})
        put(con, {"url": "https://wp.stolaf.edu/c/", "checked_at": "2026-10-28T03:00:00+00:00", "status": "gone"})
    r = TestClient(web.app).get("/api/status")
    assert r.json() == {"updated": "2026-10-27T03:00:00+00:00", "pages": 2}
