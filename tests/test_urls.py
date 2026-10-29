from pathlib import Path

import httpx

from ole.urls import Robots, is_noise, normalize, subsite_sitemap

FIX = Path(__file__).parent / "fixtures"


def test_noise():
    assert is_noise("https://wp.stolaf.edu/news/some-story/")
    assert is_noise("https://wp.stolaf.edu/calendar/event/x/")
    assert is_noise("https://wp.stolaf.edu/pipercenter/?s=intern")
    assert is_noise("https://wp.stolaf.edu/tag/alumni/")
    assert is_noise("https://wp.stolaf.edu/registrar/2019/05/")
    assert not is_noise("https://wp.stolaf.edu/pipercenter/internships/")
    assert not is_noise("https://catalog.stolaf.edu/academic-programs/computer-science/")
    assert is_noise("https://wp.stolaf.edu/NEWS/story/")
    assert is_noise("https://catalog.stolaf.edu/academic-programs/biology/biology.pdf")  # print copy of an HTML page
    assert not is_noise("https://wp.stolaf.edu/wp-content/uploads/2024/05/handbook.pdf")


def _robots(status=200, body=None):
    text = body if body is not None else (FIX / "robots_wp.txt").read_text()
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, text=text)

    return Robots(httpx.Client(transport=httpx.MockTransport(handler))), calls


def test_robots_wildcards():
    r, calls = _robots()
    assert not r.allowed("https://wp.stolaf.edu/news/a?b=1")
    assert not r.allowed("https://wp.stolaf.edu/catalog/x/")
    assert r.allowed("https://wp.stolaf.edu/pipercenter/")
    assert len(calls) == 1  # fetched once per host


def test_robots_missing_allows_and_5xx_blocks():
    r, _ = _robots(status=404, body="")
    assert r.allowed("https://wp.stolaf.edu/anything/")
    r, _ = _robots(status=500, body="")
    assert not r.allowed("https://wp.stolaf.edu/anything/")


def test_normalize():
    assert normalize("http://WP.stolaf.edu/a/#top") == "https://wp.stolaf.edu/a/"
    assert normalize("https://wp.stolaf.edu/classics") == "https://wp.stolaf.edu/classics/"
    assert normalize("https://wp.stolaf.edu") == "https://wp.stolaf.edu/"
    assert normalize("https://wp.stolaf.edu/wp-content/uploads/2024/05/x.pdf").endswith("x.pdf")
    assert normalize("https://catalog.stolaf.edu/search") == "https://catalog.stolaf.edu/search"


def test_subsite_sitemap():
    assert subsite_sitemap("https://wp.stolaf.edu/registrar/forms/") == "https://wp.stolaf.edu/registrar/sitemap.xml"
    assert subsite_sitemap("https://wp.stolaf.edu/") is None
    assert subsite_sitemap("https://catalog.stolaf.edu/x/") is None


def test_robots_rate_limited_raises_and_retries_later():
    import pytest
    from ole.urls import RateLimited
    state = {"limited": True}

    def handler(request):
        if state["limited"]:
            return httpx.Response(503, headers={"retry-after": "60"})
        return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")

    r = Robots(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RateLimited) as e:
        r.allowed("https://wp.stolaf.edu/a/")
    assert e.value.retry_after == 60
    state["limited"] = False
    assert r.allowed("https://wp.stolaf.edu/a/") and not r.allowed("https://wp.stolaf.edu/private/x")


def test_double_slashes_collapse_so_robots_rules_apply():
    assert normalize("https://www.stolaf.edu//apps/olecard/checkbalance/index.cfm") == \
        "https://www.stolaf.edu/apps/olecard/checkbalance/index.cfm"
    r, _ = _robots()
    assert not r.allowed(normalize("https://wp.stolaf.edu//catalog/x/"))
