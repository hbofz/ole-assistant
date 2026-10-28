import json

import httpx
import pytest
from qdrant_client import models

import ole.crawl as crawl_mod
import ole.index as index_mod
import ole.refresh as refresh_mod
from ole.db import connect, put
from ole.index import index_pages
from ole.urls import RateLimited

WP = "https://wp.stolaf.edu"
GDOC = "https://docs.google.com/document/d/H1/edit"
INDEX = f"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{WP}/sitemap-1.xml</loc><lastmod>2026-10-01T00:00:00Z</lastmod></sitemap>
<sitemap><loc>{WP}/sitemap-2.xml</loc><lastmod>2026-10-27T00:00:00Z</lastmod></sitemap></sitemapindex>"""
SM2 = f"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{WP}/a/</loc><lastmod>2026-01-01</lastmod></url>
<url><loc>{WP}/b/</loc><lastmod>2026-02-01</lastmod></url>
<url><loc>{WP}/c/</loc><lastmod>2026-02-01</lastmod></url>
<url><loc>{WP}/c2/</loc><lastmod>2026-02-01</lastmod></url></urlset>"""


def html(title, words):
    return f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1><p>{words * 30}</p></main></body></html>"


def site(reqs, overrides=None):
    pages = {f"{WP}/robots.txt": "User-agent: *\nDisallow:\n", f"{WP}/sitemap.xml": INDEX, f"{WP}/sitemap-2.xml": SM2,
             f"{WP}/b/": html("B", "brand new words "), f"{WP}/c/": html("C", "a new page "),
             f"{WP}/c2/": html("C2", "another new page "), f"{WP}/e/": html("E", "steady words "),
             f"{WP}/f2/": html("F2", "moved here "), "https://docs.google.com/document/d/H1/export?format=txt": "Doc H\n\nSame."}

    def handler(req):
        url = str(req.url)
        reqs.append(url)
        if overrides and url in overrides:
            return overrides[url](req)
        if url == f"{WP}/f/":
            return httpx.Response(301, headers={"location": f"{WP}/f2/"})
        if url in pages:
            ctype = "text/plain" if "docs.google" in url else "text/html"
            return httpx.Response(200, text=pages[url], headers={"content-type": ctype})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def world(tmp_path, monkeypatch, qd):
    """A db that was crawled and indexed a while ago, and the same site today."""
    client, _ = qd
    monkeypatch.setattr(crawl_mod, "RAW_DIR", tmp_path)
    monkeypatch.setattr(crawl_mod, "GOOGLE_PENDING", tmp_path / "pending.jsonl")
    monkeypatch.setattr(crawl_mod, "CRAWL_DELAY", 0)
    monkeypatch.setattr(refresh_mod, "SEED_SITEMAPS", [f"{WP}/sitemap.xml"])
    monkeypatch.setattr(refresh_mod, "SITEMAPS", tmp_path / "sitemaps.json")
    (tmp_path / "sitemaps.json").write_text(json.dumps({f"{WP}/sitemap-1.xml": "2026-10-01T00:00:00Z"}))
    old = [  # (url, kind, source, lastmod, fetched_at, body)
        (f"{WP}/a/", "html", "crawl", "2026-01-01", "2026-06-01T00:00:00+00:00", html("A", "unchanged words ")),
        (f"{WP}/b/", "html", "crawl", "2026-01-01", "2026-06-01T00:00:00+00:00", html("B", "old words ")),
        (f"{WP}/d/", "html", "crawl", None, "2020-01-01T00:00:00+00:00", html("D", "doomed words ")),
        (f"{WP}/e/", "html", "crawl", None, "2020-01-01T00:00:00+00:00", html("E", "steady words ")),
        (f"{WP}/f/", "html", "crawl", None, "2020-01-01T00:00:00+00:00", html("F", "moving words ")),
        ("https://drive.google.com/x", "text", "drive_mcp", None, "2019-01-01T00:00:00+00:00", "Drive doc"),
        (GDOC, "text", "gdoc_export", None, "2020-01-01T00:00:00+00:00", "Doc H\n\nSame."),
    ]
    rows = []
    for i, (url, kind, source, lastmod, t, body) in enumerate(old):
        (tmp_path / f"{i}.raw").write_text(body)
        rows.append({"url": url, "file": f"{i}.raw", "kind": kind, "source": source, "title": "Doc H" if url == GDOC
                     else "T", "lastmod": lastmod, "fetched_at": t, "checked_at": t})
    index_pages(rows, client)
    return client


def db_rows():
    with connect() as con:
        return {r["url"]: dict(r) for r in con.execute("SELECT * FROM pages")}


def n_points(client, url):
    return client.count(index_mod.COLLECTION, models.Filter(must=[models.FieldCondition(
        key="url", match=models.MatchValue(value=url))])).count


def test_refresh(world, tmp_path, monkeypatch):
    client, reqs, sleeps = world, [], []
    monkeypatch.setattr(refresh_mod.time, "sleep", sleeps.append)
    blip = {"n": 0}

    def e_once_503(req):  # a firewall blip is retried, never a removal
        blip["n"] += 1
        if blip["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "60"})
        return httpx.Response(200, text=html("E", "steady words "), headers={"content-type": "text/html"})

    before = db_rows()
    out = refresh_mod.Refresher(site(reqs, {f"{WP}/e/": e_once_503}), client, checks=4, new=10).run()

    assert out["added"] == [f"{WP}/c/", f"{WP}/c2/", f"{WP}/f2/"]
    assert out["changed"] == [f"{WP}/b/"]
    assert out["gone"] == [f"{WP}/d/", f"{WP}/f/"]
    assert out["checked"] == 4 and out["drive_mcp"] == 1 and out["errors"] == 0
    assert 60 in sleeps
    # unchanged lastmod: no request, but checked; skipped child sitemap: no request
    assert f"{WP}/a/" not in reqs and f"{WP}/sitemap-1.xml" not in reqs and "https://drive.google.com/x" not in reqs
    rows = db_rows()
    assert rows[f"{WP}/a/"]["checked_at"] > before[f"{WP}/a/"]["checked_at"]
    assert rows[f"{WP}/a/"]["fetched_at"] == before[f"{WP}/a/"]["fetched_at"]
    assert rows[f"{WP}/b/"]["lastmod"] == "2026-02-01" and rows[f"{WP}/b/"]["hash"] != before[f"{WP}/b/"]["hash"]
    assert rows[f"{WP}/e/"]["hash"] == before[f"{WP}/e/"]["hash"] and rows[f"{WP}/e/"]["status"] == "live"
    assert rows[f"{WP}/e/"]["fetched_at"] > before[f"{WP}/e/"]["fetched_at"]
    assert rows[GDOC]["hash"] == before[GDOC]["hash"]
    assert rows[f"{WP}/d/"]["status"] == rows[f"{WP}/f/"]["status"] == "gone"
    assert n_points(client, f"{WP}/d/") == n_points(client, f"{WP}/f/") == 0
    assert n_points(client, f"{WP}/c/") > 0 and n_points(client, f"{WP}/f2/") > 0
    saved = json.loads((tmp_path / "sitemaps.json").read_text())
    assert saved == {f"{WP}/sitemap-1.xml": "2026-10-01T00:00:00Z", f"{WP}/sitemap-2.xml": "2026-10-27T00:00:00Z"}

    reqs.clear()  # second night: nothing changed, so sitemap-2 is skipped too
    out = refresh_mod.Refresher(site(reqs), client, checks=0, new=10).run()
    assert out["added"] == out["changed"] == out["gone"] == [] and f"{WP}/sitemap-2.xml" not in reqs


def test_new_pages_capped_and_their_sitemap_reread(world, tmp_path):
    reqs = []
    out = refresh_mod.Refresher(site(reqs), world, checks=0, new=1).run()
    assert out["added"] == [f"{WP}/c/"] and out["deferred"] == 1 and f"{WP}/c2/" not in reqs
    assert f"{WP}/sitemap-2.xml" not in json.loads((tmp_path / "sitemaps.json").read_text())


def test_blocked_night_aborts_without_sleeping(world, tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(refresh_mod.time, "sleep", sleeps.append)
    blocked = {f"{WP}/b/": lambda req: httpx.Response(503, headers={"retry-after": "3600"})}
    with pytest.raises(RateLimited):
        refresh_mod.Refresher(site([], blocked), world, checks=4, new=10).run()
    assert 3600 not in sleeps
    assert db_rows()[f"{WP}/b/"]["status"] == "live"
    assert f"{WP}/sitemap-2.xml" not in json.loads((tmp_path / "sitemaps.json").read_text())


def test_unindexed_crawl_rows_get_embedded(world):
    with connect() as con:
        put(con, {"url": f"{WP}/a/", "file": "0.raw", "kind": "html", "source": "crawl", "title": "A",
                  "lastmod": "2026-01-01", "fetched_at": "2026-06-01T00:00:00+00:00"})  # hash NULL, as the crawl leaves it
    refresh_mod.Refresher(site([]), world, checks=0, new=0).run()
    assert db_rows()[f"{WP}/a/"]["hash"]
