import re
from urllib.parse import urldefrag, urlsplit, urlunsplit

import httpx
from protego import Protego

from ole.config import USER_AGENT

NOISE = re.compile(
    r"/(news|news-archive|news-search|calendar|events?|tag|category|author|feed|wp-json)/"
    r"|/page/\d+/|/\d{4}/\d{2}/|wp-login|wp-admin|/xmlrpc\.php"
)


def normalize(url: str) -> str:
    s = urlsplit(urldefrag(url)[0])
    scheme = "https" if s.scheme in ("http", "https") else s.scheme
    host, path = s.netloc.lower(), s.path or "/"
    if host == "wp.stolaf.edu" and not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"  # WordPress serves /classics and /classics/ as the same page
    return urlunsplit((scheme, host, path, s.query, ""))


def is_noise(url: str) -> bool:
    s = urlsplit(url)
    path = s.path.lower()
    if s.query:
        return True
    if s.netloc == "catalog.stolaf.edu" and path.endswith(".pdf"):
        return True  # print copies of catalog pages we already have as HTML
    if "/wp-content/uploads/" in path:
        return not path.endswith(".pdf")  # uploaded PDFs are content; their /YYYY/MM/ path isn't an archive
    return bool(NOISE.search(path))


def subsite_sitemap(url: str) -> str | None:
    s = urlsplit(url)
    seg = s.path.strip("/").split("/")[0]
    if s.netloc != "wp.stolaf.edu" or not seg or "." in seg:
        return None
    return f"https://wp.stolaf.edu/{seg}/sitemap.xml"


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


def check_limit(r: httpx.Response) -> None:
    # St. Olaf's WordPress firewall answers 503 + Retry-After when we go too fast; honor it
    if r.status_code in (429, 503):
        ra = r.headers.get("retry-after", "")
        raise RateLimited(min(int(ra), 3600) if ra.isdigit() else 300)


class Robots:
    def __init__(self, client: httpx.Client):
        self.client = client
        self.rules: dict[str, Protego | None] = {}  # None = host blocked (robots.txt 5xx or unreachable)

    def allowed(self, url: str) -> bool:
        s = urlsplit(url)
        host = f"{s.scheme}://{s.netloc}"
        if host not in self.rules:
            self.rules[host] = self._fetch(host)
        rules = self.rules[host]
        return rules is not None and rules.can_fetch(url, USER_AGENT)

    def _fetch(self, host: str) -> Protego | None:
        try:
            r = self.client.get(f"{host}/robots.txt", headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        except httpx.HTTPError:
            return None
        check_limit(r)  # raises, so the host is asked again after the pause
        if r.status_code >= 500:
            return None
        return Protego.parse(r.text if r.status_code == 200 else "")
