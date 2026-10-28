"""data/pages.db: one row per page. hash = sha256 of what Qdrant holds for it (NULL = not indexed yet).

Run once: uv run python -m ole.db   (imports data/manifest.jsonl, keeps it as manifest.jsonl.bak)
"""
import fcntl
import json
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from ole.config import DB, LOCK, MANIFEST, RAW_DIR
from ole.urls import normalize

COLS = ("url", "file", "kind", "source", "title", "lastmod", "hash", "fetched_at", "checked_at", "status")
SCHEMA = """CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, file TEXT, kind TEXT, source TEXT, title TEXT,
    lastmod TEXT, hash TEXT, fetched_at TEXT, checked_at TEXT, status TEXT NOT NULL DEFAULT 'live')"""


def now(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    """Opened per call (never at import) so tests can point DB elsewhere; commits on success."""
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")  # the web server reads while a crawl writes
        con.execute(SCHEMA)
        with con:
            yield con
    finally:
        con.close()


def put(con, row: dict) -> None:
    con.execute(f"INSERT OR REPLACE INTO pages ({', '.join(COLS)}) VALUES ({', '.join(':' + c for c in COLS)})",
                {c: None for c in COLS} | {"status": "live"} | row)


def save_page(url: str, file: str, kind: str, source: str, title: str, lastmod: str | None) -> None:
    """A fresh fetch by the crawl or add_doc; hash NULL so the next ole.index embeds it."""
    t = now()
    with connect() as con:
        put(con, dict(url=url, file=file, kind=kind, source=source, title=title, lastmod=lastmod, fetched_at=t,
                      checked_at=t))


def mark_gone(url: str, lastmod: str | None = None) -> None:
    """Kept as a row so links and resumes don't fetch it again; its points must already be deleted."""
    with connect() as con:
        con.execute("INSERT INTO pages (url, lastmod, status, checked_at) VALUES (?, ?, 'gone', ?) ON CONFLICT(url) "
                    "DO UPDATE SET status='gone', hash=NULL, checked_at=excluded.checked_at, "
                    "lastmod=coalesce(excluded.lastmod, lastmod)", (url, lastmod, now()))


@contextmanager
def lock():
    with open(LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"another crawl, index or refresh holds {LOCK}; try again when it finishes")
        yield


def read_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:  # a line cut off by Ctrl+C mid-write
            continue
    return rows


def migrate() -> tuple[int, int, list[str]]:
    """manifest.jsonl -> pages.db. Returns (pages imported, rows merged into a twin, URLs renamed by normalize())."""
    rows = read_jsonl(MANIFEST)
    if not rows:
        return 0, 0, []
    pages = {}
    for r in rows:  # later rows win, since the crawl appended them later
        path = RAW_DIR / r["file"]
        t = now(path.stat().st_mtime) if path.exists() else now()
        pages[normalize(r["url"])] = {**r, "url": normalize(r["url"]), "fetched_at": t, "checked_at": t}
    with connect() as con:
        for p in pages.values():
            put(con, p)
    MANIFEST.rename(MANIFEST.with_name(MANIFEST.name + ".bak"))
    return len(pages), len(rows) - len(pages), sorted({r["url"] for r in rows} - set(pages))


if __name__ == "__main__":
    from qdrant_client import QdrantClient

    from ole.config import QDRANT_URL
    from ole.index import delete_points

    with lock():
        n, merged, renamed = migrate()
        print(f"[migrate] {n} pages imported, {merged} twin rows merged, {len(renamed)} URLs renamed")
        if renamed:  # their old points would otherwise linger beside the re-embedded ones
            delete_points(QdrantClient(QDRANT_URL, timeout=120), renamed)
