import os


def test_env_loaded():
    import ole.config  # noqa: F401

    assert os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-")


def test_today_can_be_pinned(monkeypatch):
    from datetime import date
    from ole.config import today
    monkeypatch.delenv("OLE_TODAY", raising=False)
    assert today() == date.today()
    monkeypatch.setenv("OLE_TODAY", "2026-10-29")
    assert today() == date(2026, 10, 29)
