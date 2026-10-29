import json
import subprocess

import pytest

import ole.claude_cli as cli


def fake_run(outputs, seen):
    def run(cmd, **kw):
        seen.append((cmd, kw))
        out = outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")
    return run


def test_ask_uses_subscription_and_returns_structured_output(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    seen = []
    monkeypatch.setattr(cli.subprocess, "run", fake_run([{"is_error": False, "structured_output": {"a": 1}}], seen))
    assert cli.ask("page text", "be terse", {"type": "object"}, model="claude-haiku-5-5") == {"a": 1}
    cmd, kw = seen[0]
    assert "ANTHROPIC_API_KEY" not in kw["env"]  # the key would bill the API instead of the subscription
    assert kw["input"] == "page text" and kw["timeout"]
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert cmd[cmd.index("--system-prompt") + 1] == "be terse"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == {"type": "object"}
    assert cmd[cmd.index("--tools") + 1] == ""


def test_ask_retries_once_then_raises(monkeypatch):
    seen = []
    monkeypatch.setattr(cli.subprocess, "run", fake_run(
        [subprocess.TimeoutExpired("claude", 1), {"is_error": False, "structured_output": {"ok": True}}], seen))
    assert cli.ask("x", "s", {}) == {"ok": True} and len(seen) == 2

    monkeypatch.setattr(cli.subprocess, "run", fake_run(
        [{"is_error": True, "result": "rate limited"}, {"is_error": True, "result": "rate limited"}], seen := []))
    with pytest.raises(RuntimeError, match="rate limited"):
        cli.ask("x", "s", {})
    assert len(seen) == 2
