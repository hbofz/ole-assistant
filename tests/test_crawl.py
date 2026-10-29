import json

import httpx

import ole.crawl as crawl_mod
from ole.db import connect

SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://wp.stolaf.edu/a/</loc><lastmod>2025-01-01</lastmod></url>
<url><loc>https://wp.stolaf.edu/b/</loc></url>
<url><loc>https://wp.stolaf.edu/c/</loc></url>
<url><loc>https://wp.stolaf.edu/news/story/</loc></url>
</urlset>"""
LINKED = '<html><head><title>A</title></head><body><a href="/d/">deep</a></body></html>'
PAGE = '<html><head><title>P - St. Olaf</title></head><body><a href="https://docs.google.com/document/d/DOC1/edit">doc</a><p>hi</p></body></html>'


def setup(tmp_path, monkeypatch, requests, linked=False):
    monkeypatch.setattr(crawl_mod, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(crawl_mod, "GOOGLE_PENDING", tmp_path / "pending.jsonl")
    monkeypatch.setattr(crawl_mod, "SEED_SITEMAPS", ["https://wp.stolaf.edu/sitemap.xml"])
    monkeypatch.setattr(crawl_mod, "SEED_PAGES", [])
    monkeypatch.setattr(crawl_mod, "CRAWL_DELAY", 0)

    def handler(req):
        url = str(req.url)
        requests.append(url)
        if url.endswith("robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        if url == "https://wp.stolaf.edu/sitemap.xml":
            return httpx.Response(200, text=SITEMAP)
        if url == "https://wp.stolaf.edu/b/":
            return httpx.Response(500)
        if "docs.google.com" in url:
            return httpx.Response(302, headers={"location": "https://accounts.google.com/login"})
        if "accounts.google.com" in url:
            return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})
        if url == "https://wp.stolaf.edu/a/" and linked:
            return httpx.Response(200, text=LINKED, headers={"content-type": "text/html"})
        if url in ("https://wp.stolaf.edu/a/", "https://wp.stolaf.edu/c/", "https://wp.stolaf.edu/d/"):
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def manifest(tmp_path, status="live"):
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM pages WHERE status=?", (status,))]


def test_crawl_resumes_and_survives_errors(tmp_path, monkeypatch):
    reqs = []
    crawl_mod.crawl(setup(tmp_path, monkeypatch, reqs))
    urls = {r["url"] for r in manifest(tmp_path)}
    assert urls == {"https://wp.stolaf.edu/a/", "https://wp.stolaf.edu/c/"}
    assert not any("/news/" in u for u in reqs)
    row = next(r for r in manifest(tmp_path) if r["url"].endswith("/a/"))
    assert row["lastmod"] == "2025-01-01" and row["kind"] == "html" and row["source"] == "crawl"
    assert (tmp_path / "raw" / row["file"]).exists()
    pending = [json.loads(l) for l in (tmp_path / "pending.jsonl").read_text().splitlines()]
    assert [p["url"] for p in pending] == ["https://docs.google.com/document/d/DOC1/edit"]

    reqs2 = []
    crawl_mod.crawl(setup(tmp_path, monkeypatch, reqs2))
    assert "https://wp.stolaf.edu/a/" not in reqs2 and "https://wp.stolaf.edu/c/" not in reqs2
    assert len(manifest(tmp_path)) == 2
    assert len((tmp_path / "pending.jsonl").read_text().splitlines()) == 1


def test_resume_keeps_link_discovered_frontier(tmp_path, monkeypatch):
    crawl_mod.crawl(setup(tmp_path, monkeypatch, [], linked=True), max_pages=1)
    assert {r["url"] for r in manifest(tmp_path)} == {"https://wp.stolaf.edu/a/"}
    crawl_mod.crawl(setup(tmp_path, monkeypatch, [], linked=True))
    assert "https://wp.stolaf.edu/d/" in {r["url"] for r in manifest(tmp_path)}


def test_backs_off_when_rate_limited(tmp_path, monkeypatch):
    sleeps, state = [], {"limited": True}
    client = setup(tmp_path, monkeypatch, [])
    inner = client._transport.handler

    def handler(req):
        if str(req.url) == "https://wp.stolaf.edu/c/" and state["limited"]:
            state["limited"] = False
            return httpx.Response(503, headers={"retry-after": "120"})
        return inner(req)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(crawl_mod.time, "sleep", sleeps.append)
    c = crawl_mod.Crawler(client, 100)
    crawl_mod.RAW_DIR.mkdir(parents=True, exist_ok=True)
    c.run()
    assert "https://wp.stolaf.edu/c/" in {r["url"] for r in manifest(tmp_path)}
    assert 120 in sleeps
    assert c.delay > crawl_mod.CRAWL_DELAY


def test_public_gdoc_via_redirect_and_pending_retry(tmp_path, monkeypatch):
    client = setup(tmp_path, monkeypatch, [])
    inner = client._transport.handler
    (tmp_path / "pending.jsonl").write_text(json.dumps({"url": "https://docs.google.com/document/d/OLD1/edit", "found_on": None}) + "\n")

    def handler(req):
        url = str(req.url)
        if "/document/d/DOC1/export" in url or "/document/d/OLD1/export" in url:
            return httpx.Response(307, headers={"location": "https://doc-0k.googleusercontent.com/export/abc"})
        if "googleusercontent.com" in url:
            return httpx.Response(200, text="﻿Internship Handbook\n\nStep one.", headers={"content-type": "text/plain; charset=utf-8"})
        return inner(req)

    crawl_mod.crawl(httpx.Client(transport=httpx.MockTransport(handler)))
    rows = {r["url"]: r for r in manifest(tmp_path)}
    for doc in ("DOC1", "OLD1"):
        row = rows[f"https://docs.google.com/document/d/{doc}/edit"]
        assert row["source"] == "gdoc_export" and row["kind"] == "text" and row["title"] == "Internship Handbook"
    assert (tmp_path / "pending.jsonl").read_text().strip() == ""


def test_link_404_gets_gone_row_and_is_not_refetched(tmp_path, monkeypatch):
    reqs = []
    inner = setup(tmp_path, monkeypatch, reqs, linked=True)._transport.handler  # /a/ links to /d/

    def handler(req):
        return httpx.Response(404) if str(req.url) == "https://wp.stolaf.edu/d/" else inner(req)

    crawl_mod.crawl(httpx.Client(transport=httpx.MockTransport(handler)))
    assert [r["url"] for r in manifest(tmp_path, "gone")] == ["https://wp.stolaf.edu/d/"]
    reqs.clear()
    crawl_mod.crawl(httpx.Client(transport=httpx.MockTransport(handler)))
    assert "https://wp.stolaf.edu/d/" not in reqs


def test_redirect_into_disallowed_path_not_saved_and_no_refetch(tmp_path, monkeypatch):
    reqs = []
    client = setup(tmp_path, monkeypatch, reqs)
    inner = client._transport.handler

    def handler(req):
        url = str(req.url)
        if url == "https://wp.stolaf.edu/c/":
            reqs.append(url)
            return httpx.Response(301, headers={"location": "https://wp.stolaf.edu/private/c/"})
        return inner(req)

    crawl_mod.crawl(httpx.Client(transport=httpx.MockTransport(handler)))
    urls = {r["url"] for r in manifest(tmp_path)}
    assert "https://wp.stolaf.edu/private/c/" not in urls and "https://wp.stolaf.edu/c/" not in urls
    assert reqs.count("https://wp.stolaf.edu/a/") == 1


def test_shallow_pages_first(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, [])
    c = crawl_mod.Crawler(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))), 10)
    for u in ["https://wp.stolaf.edu/a/b/c/d/", "https://wp.stolaf.edu/x/", "https://wp.stolaf.edu/a/b/"]:
        c.enqueue(u)
    assert [c.next_page() for _ in range(3)] == ["https://wp.stolaf.edu/x/", "https://wp.stolaf.edu/a/b/",
                                                  "https://wp.stolaf.edu/a/b/c/d/"]


def test_redirect_to_login_recorded_and_not_refetched(tmp_path, monkeypatch):
    reqs = []

    def make_client():
        inner = setup(tmp_path, monkeypatch, reqs)._transport.handler

        def handler(req):
            url = str(req.url)
            if url == "https://wp.stolaf.edu/c/":
                reqs.append(url)
                return httpx.Response(302, headers={"location": "https://wp.stolaf.edu/wp-login.php?redirect_to=x"})
            if "wp-login.php" in url:
                return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})
            return inner(req)
        return httpx.Client(transport=httpx.MockTransport(handler))

    crawl_mod.crawl(make_client())
    assert "https://wp.stolaf.edu/c/" in {r["url"] for r in manifest(tmp_path, status="gone")}
    before = reqs.count("https://wp.stolaf.edu/c/")
    crawl_mod.crawl(make_client())
    assert reqs.count("https://wp.stolaf.edu/c/") == before  # second run doesn't ask again


def test_login_walled_subsite_skipped_after_first_redirect(tmp_path, monkeypatch):
    reqs = []
    inner = setup(tmp_path, monkeypatch, reqs)._transport.handler
    sitemap = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://wp.stolaf.edu/vids/one/</loc></url><url><loc>https://wp.stolaf.edu/vids/two/</loc></url>
<url><loc>https://wp.stolaf.edu/vids/three/</loc></url></urlset>"""

    def handler(req):
        url = str(req.url)
        if url == "https://wp.stolaf.edu/sitemap.xml":
            return httpx.Response(200, text=sitemap)
        if url.startswith("https://wp.stolaf.edu/vids/") and "wp-login" not in url and "sitemap" not in url:
            reqs.append(url)
            return httpx.Response(302, headers={"location": "https://wp.stolaf.edu/vids/wp-login.php?redirect_to=x"})
        if "wp-login.php" in url:
            return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})
        return inner(req)

    crawl_mod.crawl(httpx.Client(transport=httpx.MockTransport(handler)))
    assert [u for u in reqs if "/vids/" in u and "sitemap" not in u] == ["https://wp.stolaf.edu/vids/one/"]  # the other two are known to be behind the same login
    assert {"https://wp.stolaf.edu/vids/two/", "https://wp.stolaf.edu/vids/three/"} <= \
        {r["url"] for r in manifest(tmp_path, status="gone")}
