from pathlib import Path

from ole.sitemaps import parse_sitemap

FIX = Path(__file__).parent / "fixtures"


def test_parse_sitemap_index_skips_media():
    children, urls = parse_sitemap((FIX / "sitemap_index.xml").read_text())
    assert children == [("https://wp.stolaf.edu/sitemap-index-1.xml", "2026-10-27T20:10:36Z")]
    assert urls == []


def test_parse_sitemap_urlset_lastmod():
    children, urls = parse_sitemap((FIX / "sitemap_urlset.xml").read_text())
    assert children == []
    assert ("https://wp.stolaf.edu/registrar/codescguidecepcapp/", "2023-06-07T13:34:28Z") in urls
    assert len(urls) > 50


def test_parse_garbage_is_empty():
    assert parse_sitemap("<html>not a sitemap</html>") == ([], [])
