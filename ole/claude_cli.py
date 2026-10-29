"""Headless Claude Code on Hamzah's subscription, for offline jobs (conflicts, eval grading).

The chat itself stays on the API: it serves other people, so it must not run on a personal subscription.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")  # systemd's PATH lacks ~/.local/bin
# no tools, settings, skills or MCP servers: without these flags each call carries ~50K tokens of context
FLAGS = ["--output-format", "json", "--no-session-persistence", "--tools", "", "--disable-slash-commands",
         "--strict-mcp-config", "--setting-sources", ""]


def ask(prompt: str, system: str, schema: dict, model: str = "claude-haiku-5-5", effort: str = "low",
        timeout: int = 600) -> dict:
    """One structured-output call (prompt on stdin). Retries once, then raises RuntimeError."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # else it bills the API key
    cmd = [CLAUDE, "-p", "--model", model, "--effort", effort, *FLAGS, "--system-prompt", system,
           "--json-schema", json.dumps(schema)]
    err = None
    for _ in range(2):
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, env=env)
            out = json.loads(r.stdout)
            if not out.get("is_error") and out.get("structured_output") is not None:
                return out["structured_output"]
            err = out.get("result") or out.get("subtype")
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            err = f"{type(e).__name__}: {e}"
    raise RuntimeError(f"claude -p failed: {str(err)[:300]}")
