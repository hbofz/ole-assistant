"""Polite crawler: sitemaps + in-scope links -> data/raw + data/pages.db.

Run: uv run python -m ole.crawl
Resumable: URLs already in pages.db (including gone ones) are never fetched again; ole.refresh re-checks them.
"""
import hashlib
import heapq
import json
import os
import re
import time
from collections import deque
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from ole.config import (ALLOWED_HOSTS, CRAWL_DELAY, GOOGLE_PENDING, MAX_PAGES, RAW_DIR, SEED_PAGES, SEED_SITEMAPS,
                        USER_AGENT)
from ole.db import connect, lock, mark_gone, read_jsonl, save_page
from ole.extract import extract_links, page_title
from ole.sitemaps import parse_sitemap
from ole.urls import RateLimited, Robots, is_noise, normalize, subsite_sitemap
from ole.urls import check_limit as _check_limit

GDOC = re.compile(r"https://docs\.google\.com/document/d/([\w-]+)")
GDOC_EXPORT = "https://docs.google.com/document/d/{}/export?format=txt"
GOOGLE_OTHER = re.compile(r"https://(docs\.google\.com/(spreadsheets|presentation)/d/|drive\.google\.com/(file/d/|open\?))")
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_DELAY = 10.0
EXT = {"html": "html", "pdf": "pdf", "text": "txt"}


def _append(path, row: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _subsite(url: str) -> str:
    s = urlsplit(url)
    return f"{s.netloc}/{s.path.strip('/').split('/')[0]}"


class Crawler:
    def __init__(self, client: httpx.Client, max_pages: int):
        self.client = client
        self.max_pages = max_pages
        self.robots = Robots(client)
        # end a half-written last line so new rows aren't glued onto it
        if GOOGLE_PENDING.exists() and GOOGLE_PENDING.stat().st_size and \
                not GOOGLE_PENDING.read_bytes().endswith(b"\n"):
            with GOOGLE_PENDING.open("a") as f:
                f.write("\n")
        with connect() as con:
            rows = con.execute("SELECT url, status FROM pages").fetchall()
        self.done = {r["url"] for r in rows}
        self.saved = sum(r["status"] == "live" for r in rows)
        self.google = {r["url"] for r in read_jsonl(GOOGLE_PENDING)} | {u for u in self.done if "google.com" in u}
        self.seen = set(self.done)
        self.lastmod: dict[str, str | None] = {}
        self.queue: list[tuple[int, int, str]] = []  # heap of (path depth, order, url): shallow pages first
        self.order = 0
        self.sitemaps: deque[str] = deque(SEED_SITEMAPS)
        self.seen_sitemaps: set[str] = set()
        self.last_request = 0.0
        self.delay = CRAWL_DELAY
        self.errors = 0
        self.walled: set[str] = set()  # wp.stolaf.edu sub-sites that redirect to their login page

    def get(self, url: str) -> httpx.Response:
        wait = self.last_request + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return self.client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30)
        finally:
            self.last_request = time.monotonic()

    def enqueue(self, url: str, found_on: str | None = None) -> None:
        url = normalize(url)
        if GDOC.match(url) or GOOGLE_OTHER.match(url):
            self.google_link(url, found_on)
            return
        host = urlsplit(url).netloc
        if url in self.seen or host not in ALLOWED_HOSTS or is_noise(url):
            return
        self.seen.add(url)
        sm = subsite_sitemap(url)
        if sm and sm not in self.seen_sitemaps:
            self.sitemaps.append(sm)
        self.push(url)

    def push(self, url: str, front: bool = False) -> None:
        depth = len([p for p in urlsplit(url).path.split("/") if p])
        self.order += 1
        heapq.heappush(self.queue, (-1 if front else depth, self.order, url))

    def next_page(self) -> str:
        return heapq.heappop(self.queue)[2]

    def google_link(self, url: str, found_on: str | None) -> None:
        m = GDOC.match(url)
        canonical = f"https://docs.google.com/document/d/{m.group(1)}/edit" if m else url.split("#")[0]
        if canonical in self.google:
            return
        self.google.add(canonical)
        if not (m and self.export_gdoc(m.group(1), canonical)):
            _append(GOOGLE_PENDING, {"url": canonical, "found_on": found_on})

    def export_gdoc(self, doc_id: str, canonical: str) -> bool:
        """Public ("anyone with the link") Docs export as plain text; Google redirects to googleusercontent.com."""
        try:
            r = self.get(GDOC_EXPORT.format(doc_id))
        except httpx.HTTPError:
            return False
        return self.save_gdoc(canonical, r)

    def save_gdoc(self, canonical: str, r: httpx.Response) -> bool:
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("text/plain") or \
                not r.url.host.endswith(("docs.google.com", "googleusercontent.com")):
            return False
        text = r.text.lstrip("\ufeff")
        title = next((l.strip() for l in text.splitlines() if l.strip()), "Google Doc")[:120]
        self.save(canonical, text.encode(), "text", "gdoc_export", title)
        return True

    def retry_pending_google(self) -> None:
        keep = []
        for row in read_jsonl(GOOGLE_PENDING):
            m = GDOC.match(row["url"])
            if not (m and row["url"] not in self.done and self.export_gdoc(m.group(1), row["url"])):
                keep.append(row)
        tmp = GOOGLE_PENDING.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in keep))
        os.replace(tmp, GOOGLE_PENDING)  # atomic, so Ctrl+C can't lose the list

    def write_raw(self, url: str, body: bytes, kind: str) -> str:
        name = f"{hashlib.sha1(url.encode()).hexdigest()}.{EXT[kind]}"
        (RAW_DIR / name).write_bytes(body)
        return name

    def save(self, url: str, body: bytes, kind: str, source: str, title: str) -> None:
        save_page(url, self.write_raw(url, body, kind), kind, source, title, self.lastmod.get(url))
        self.done.add(url)
        self.saved += 1
        if self.saved % 50 == 0:
            print(f"[crawl] {self.saved} saved, {len(self.queue)} queued, {self.errors} errors", flush=True)

    def do_sitemap(self, sm: str) -> None:
        self.seen_sitemaps.add(sm)
        if not self.robots.allowed(sm):
            return
        r = self.get(sm)
        _check_limit(r)
        if r.status_code != 200:
            return
        children, urls = parse_sitemap(r.text)
        self.sitemaps.extend(c for c, _ in children if c not in self.seen_sitemaps)
        for url, lastmod in urls:
            self.lastmod[normalize(url)] = lastmod
            self.enqueue(url)

    def do_page(self, url: str) -> None:
        if url in self.done or not self.robots.allowed(url):
            return
        if _subsite(url) in self.walled:  # same login wall as an earlier page: record without asking
            mark_gone(url, self.lastmod.get(url))
            self.done.add(url)
            return
        r = self.get(url)
        _check_limit(r)
        final = normalize(str(r.url))
        if r.status_code in (404, 410):
            mark_gone(url, self.lastmod.get(url))
            self.done.add(url)
            return
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        if final != url:
            if urlsplit(final).path.endswith("/wp-login.php") and _subsite(final) == _subsite(url):
                self.walled.add(_subsite(url))
            # record the redirect source so no later run fetches it again (login walls, moved pages)
            mark_gone(url, self.lastmod.get(url))
            self.done.add(url)
            if final in self.done or urlsplit(final).netloc not in ALLOWED_HOSTS or is_noise(final) \
                    or not self.robots.allowed(final):
                return
        self.lastmod.setdefault(final, self.lastmod.get(url))
        ctype = r.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            html = r.text
            self.save(final, html.encode(), "html", "crawl", page_title(BeautifulSoup(html, "html.parser")))
            for link in extract_links(html, final):
                self.enqueue(link, found_on=final)
        elif ctype.startswith("application/pdf") and len(r.content) <= MAX_PDF_BYTES:
            self.save(final, r.content, "pdf", "crawl", final.rsplit("/", 1)[-1])

    def run(self) -> None:
        self.retry_pending_google()
        for url in SEED_PAGES:
            self.enqueue(url)
        # on resume, rebuild the link frontier from cached pages (no network)
        with connect() as con:
            rows = con.execute("SELECT url, file FROM pages WHERE kind='html' AND status='live'").fetchall()
        for row in rows:
            html = (RAW_DIR / row["file"]).read_text(errors="replace")
            for link in extract_links(html, row["url"]):
                self.enqueue(link, found_on=row["url"])
        while (self.sitemaps or self.queue) and self.saved < self.max_pages:
            # sitemaps first so lastmod dates are known before their pages are fetched
            target, handler = (self.sitemaps.popleft(), self.do_sitemap) if self.sitemaps else \
                (self.next_page(), self.do_page)
            if handler == self.do_sitemap and target in self.seen_sitemaps:
                continue
            try:
                handler(target)
            except RateLimited as e:
                self.delay = min(max(self.delay * 2, 2.0), MAX_DELAY)
                print(f"[crawl] rate limited; sleeping {e.retry_after}s, then {self.delay:.0f}s between requests",
                      flush=True)
                time.sleep(e.retry_after)
                if handler == self.do_sitemap:
                    self.seen_sitemaps.discard(target)
                    self.sitemaps.appendleft(target)
                else:
                    self.push(target, front=True)
            except Exception as e:  # one bad URL never stops the crawl
                self.errors += 1
                print(f"[crawl] skip {target}: {type(e).__name__}: {e}", flush=True)
        print(f"[crawl] finished: {self.saved} saved, {self.errors} errors, {len(self.google)} google links", flush=True)


def crawl(client: httpx.Client, max_pages: int = MAX_PAGES) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    Crawler(client, max_pages).run()


if __name__ == "__main__":
    with lock(), httpx.Client() as c:
        crawl(c)
