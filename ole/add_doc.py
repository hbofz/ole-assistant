"""Register a document fetched by hand (e.g. a Google Doc read through the Drive MCP).

Run: uv run python -m ole.add_doc --url URL --title TITLE --file PATH   (then uv run python -m ole.index)
"""
import argparse
import hashlib
from pathlib import Path

from ole.config import RAW_DIR
from ole.db import lock, save_page


def add_doc(url: str, title: str, text_file: Path) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{hashlib.sha1(url.encode()).hexdigest()}.txt"
    (RAW_DIR / name).write_text(Path(text_file).read_text())
    save_page(url, name, "text", "drive_mcp", title, None)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--file", required=True, type=Path)
    a = p.parse_args()
    with lock():  # ole.index rewrites rows, so a concurrent add could lose its "needs embedding" mark
        add_doc(a.url, a.title, a.file)
    print(f"added {a.url}")
