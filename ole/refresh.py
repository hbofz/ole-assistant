"""Bring pages.db and Qdrant up to date with St. Olaf's sites; cheap enough to run nightly. Search stays up.

Run: uv run python -m ole.refresh [--checks 300] [--new 300]
1. Sitemaps (seeds plus every wp.stolaf.edu sub-site in the db): pages that are new or whose lastmod differs
   get fetched; the rest only get checked_at. Child sitemaps whose lastmod hasn't moved since they were last
   read in full are skipped.
2. Rotation: re-GET the --checks pages fetched longest ago, for changes lastmod misses (widgets, PDFs, Google
   Docs). Drive-MCP docs are skipped (manual, see ole.add_doc).
3. 404/410, a redirect, or a robots.txt ban marks a page gone and deletes its points. A Retry-After of 300 s
   or more aborts the run (exit 1); the next run carries on.
Only text that changed is re-embedded. New links on pages are left to ole.crawl.
"""
import argparse
import json
import os
import sys
import time
from collections import deque
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from qdrant_client import QdrantClient

from ole.config import ALLOWED_HOSTS, DATA_DIR, MAX_PAGES, QDRANT_URL, RAW_DIR, SEED_SITEMAPS
from ole.crawl import GDOC, GDOC_EXPORT, MAX_DELAY, MAX_PDF_BYTES, Crawler
from ole.db import connect, lock, mark_gone, now
from ole.extract import page_title
from ole.index import delete_points, ensure_collection, index_pages
from ole.sitemaps import parse_sitemap
from ole.urls import RateLimited, is_noise, normalize, subsite_sitemap
from ole.urls import check_limit as _check_limit

SITEMAPS = DATA_DIR / "sitemaps.json"  # child sitemap -> its lastmod when we last read all of it
BLOCKED = 300  # seconds of Retry-After that mean "blocked tonight"


class Refresher(Crawler):
    def __init__(self, client: httpx.Client, qdrant: QdrantClient, checks: int, new: int):
        super().__init__(client, MAX_PAGES)
        self.qdrant, self.checks, self.new = qdrant, checks, new
        with connect() as con:
            self.rows = {r["url"]: dict(r) for r in con.execute("SELECT * FROM pages")}
        self.read = json.loads(SITEMAPS.read_text()) if SITEMAPS.exists() else {}
        self.child_lastmod: dict[str, str] = {}
        self.parent: dict[str, str] = {}  # page or child sitemap -> the sitemap that listed it
        self.unfinished: set[str] = set()  # sitemaps with a page we failed or deferred: read them again next run
        self.flagged: dict[str, None] = {}  # new or lastmod differs, in sitemap order
        self.unchanged: list[str] = []
        self.touched: set[str] = set()
        self.log = {"added": [], "changed": [], "gone": []}
        self.skipped_sitemaps = 0

    def unfinish(self, url: str) -> None:
        while url := self.parent.get(url):
            self.unfinished.add(url)

    def attempt(self, fn, target: str) -> None:
        while True:
            try:
                return fn(target)
            except RateLimited as e:
                if e.retry_after >= BLOCKED:
                    raise
                self.delay = min(max(self.delay * 2, 2.0), MAX_DELAY)
                print(f"[refresh] rate limited; sleeping {e.retry_after}s", flush=True)
                time.sleep(e.retry_after)
            except Exception as e:
                self.errors += 1
                self.unfinish(target)
                print(f"[refresh] skip {target}: {type(e).__name__}: {e}", flush=True)
                return

    def do_sitemap(self, sm: str) -> None:
        self.seen_sitemaps.add(sm)
        if not self.robots.allowed(sm):
            return
        r = self.get(sm)
        _check_limit(r)
        if r.status_code != 200:
            self.unfinish(sm)
            return
        children, urls = parse_sitemap(r.text)
        for child, lastmod in children:
            if lastmod and self.read.get(child) == lastmod:
                self.skipped_sitemaps += 1
            elif child not in self.seen_sitemaps:
                self.parent[child] = sm
                self.child_lastmod[child] = lastmod
                self.sitemaps.append(child)
        for url, lastmod in urls:
            url = normalize(url)
            if urlsplit(url).netloc not in ALLOWED_HOSTS or is_noise(url):
                continue
            self.parent[url] = sm
            self.lastmod[url] = lastmod
            old = self.rows.get(url)
            if old and old["lastmod"] == lastmod:  # "differs", not "newer": formats vary and nulls exist
                self.unchanged.append(url)
            else:
                self.flagged[url] = None

    def do_page(self, url: str) -> None:
        self.touched.add(url)
        if not self.robots.allowed(url):
            return self.remove(url)
        r = self.get(url)
        _check_limit(r)  # 503 raises: never a removal
        if r.status_code in (404, 410):
            return self.remove(url)  # WordPress 404s are deterministic, so one strike
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        final = normalize(str(r.url))
        if final != url:
            self.remove(url)
            if final in self.touched or urlsplit(final).netloc not in ALLOWED_HOSTS or is_noise(final) or \
                    not self.robots.allowed(final):
                return
            url = final
            self.touched.add(url)
        ctype = r.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            self.save(url, r.text.encode(), "html", "crawl", page_title(BeautifulSoup(r.text, "html.parser")))
        elif ctype.startswith("application/pdf") and len(r.content) <= MAX_PDF_BYTES:
            self.save(url, r.content, "pdf", "crawl", url.rsplit("/", 1)[-1])
        else:
            raise RuntimeError(f"unexpected {ctype or 'content'}")

    def do_gdoc(self, url: str) -> None:
        self.touched.add(url)
        r = self.get(GDOC_EXPORT.format(GDOC.match(url).group(1)))
        if r.status_code in (404, 410) or r.url.host == "accounts.google.com":
            self.remove(url)  # deleted, or no longer public
        elif not self.save_gdoc(url, r):
            raise RuntimeError(f"HTTP {r.status_code}")

    def save(self, url: str, body: bytes, kind: str, source: str, title: str) -> None:
        old = self.rows.get(url) or {}
        t = now()
        row = {**old, "url": url, "file": self.write_raw(url, body, kind), "kind": kind,
               "source": old.get("source") or source, "title": title, "lastmod": self.lastmod.get(url, old.get("lastmod")),
               "fetched_at": t, "checked_at": t, "status": "live"}
        if index_pages([row], self.qdrant):
            was_indexed = old.get("status") == "live" and old.get("hash")
            self.log["changed" if was_indexed else "added"].append(url)
            print(f"[refresh] {'changed' if was_indexed else 'added'} {url}", flush=True)

    def remove(self, url: str) -> None:
        if self.rows.get(url, {}).get("status") == "live":
            delete_points(self.qdrant, [url])  # before the row, so a crash leaves it live and retried
            self.log["gone"].append(url)
            print(f"[refresh] gone {url}", flush=True)
        mark_gone(url, self.lastmod.get(url))

    def run(self) -> dict:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        ensure_collection(self.qdrant)
        self.sitemaps = deque([*SEED_SITEMAPS, *sorted({subsite_sitemap(u) for u in self.rows} - {None})])
        while self.sitemaps:
            sm = self.sitemaps.popleft()
            if sm not in self.seen_sitemaps:
                self.attempt(self.do_sitemap, sm)
        with connect() as con:
            con.executemany("UPDATE pages SET checked_at=? WHERE url=?", [(now(), u) for u in self.unchanged])

        new = [u for u in self.flagged if u not in self.rows]
        for u in new[self.new:]:  # a backlog (e.g. an unfinished crawl) waits for later runs or ole.crawl
            self.unfinish(u)
        for u in [u for u in self.flagged if u in self.rows] + new[:self.new]:
            if u not in self.touched:
                self.attempt(self.do_page, u)

        with connect() as con:
            rotation = con.execute("SELECT url, source FROM pages WHERE status='live' AND source != 'drive_mcp' "
                                   "ORDER BY fetched_at, url").fetchall()
            mcp = con.execute("SELECT count(*) FROM pages WHERE status='live' AND source='drive_mcp'").fetchone()[0]
        checked = 0
        for r in rotation:
            if checked >= self.checks:
                break
            if r["url"] not in self.touched:
                checked += 1
                self.attempt(self.do_gdoc if r["source"] == "gdoc_export" else self.do_page, r["url"])

        with connect() as con:  # pages the crawl added, or a failed embedding left behind
            backlog = [dict(r) for r in con.execute("SELECT * FROM pages WHERE status='live' AND hash IS NULL")]
        embedded = len(index_pages(backlog, self.qdrant)) if backlog else 0

        self.read |= {c: lm for c, lm in self.child_lastmod.items() if lm and c not in self.unfinished}
        tmp = SITEMAPS.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.read, indent=0, sort_keys=True))
        os.replace(tmp, SITEMAPS)

        out = {**self.log, "checked": checked, "errors": self.errors, "drive_mcp": mcp,
               "deferred": max(len(new) - self.new, 0), "backlog": embedded,
               "sitemaps": len(self.seen_sitemaps), "sitemaps_skipped": self.skipped_sitemaps}
        print(f"[refresh] done: {out['sitemaps']} sitemaps read ({out['sitemaps_skipped']} unchanged, skipped), "
              f"{len(self.unchanged)} pages unchanged by lastmod, {len(out['added'])} added, "
              f"{len(out['changed'])} changed, {len(out['gone'])} gone, {checked} re-checked, "
              f"{out['deferred']} new pages deferred, {embedded} crawled pages embedded, {self.errors} errors, "
              f"{mcp} drive_mcp docs not refreshed (manual)", flush=True)
        return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--checks", type=int, default=300, help="pages to re-GET in rotation (default 300)")
    p.add_argument("--new", type=int, default=300, help="most new sitemap pages to fetch (default 300)")
    a = p.parse_args()
    with lock(), httpx.Client() as http:
        try:
            Refresher(http, QdrantClient(QDRANT_URL, timeout=120), a.checks, a.new).run()
        except RateLimited as e:
            print(f"[refresh] blocked: Retry-After {e.retry_after}s; aborting, the next run carries on", flush=True)
            sys.exit(1)
