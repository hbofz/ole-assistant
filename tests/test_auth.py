from fastapi.testclient import TestClient

import ole.web as web


def client(monkeypatch, password="correct horse"):
    monkeypatch.setattr(web, "PASSWORD", password)
    monkeypatch.setattr(web, "FAIL_DELAY", 0)
    return TestClient(web.app, follow_redirects=False)


def test_open_when_no_password_set(monkeypatch):
    c = client(monkeypatch, password=None)
    assert c.get("/").status_code == 200


def test_pages_redirect_and_api_refuses_without_login(monkeypatch):
    c = client(monkeypatch)
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.post("/api/chat", json={"question": "hi"}).status_code == 401
    assert c.get("/api/status").status_code == 401
    assert c.get("/static/render.js").status_code == 401
    assert "Lefse" in c.get("/login").text


def test_wrong_password_rejected(monkeypatch):
    c = client(monkeypatch)
    r = c.post("/login", content="password=nope", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 401 and "That's not it" in r.text
    assert "lefse_session" not in r.cookies


def test_right_password_logs_in(monkeypatch):
    c = client(monkeypatch)
    r = c.post("/login", content="password=correct+horse", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert c.get("/", headers={"cookie": f"lefse_session={r.cookies['lefse_session']}"}).status_code == 200


def test_forged_or_stale_cookie_rejected(monkeypatch):
    c = client(monkeypatch)
    assert c.get("/", headers={"cookie": "lefse_session=forged"}).status_code == 303
    token = web.session_token("old password")
    assert c.get("/", headers={"cookie": f"lefse_session={token}"}).status_code == 303  # changing the password logs everyone out
