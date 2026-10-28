import xml.etree.ElementTree as ET

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def parse_sitemap(xml: str) -> tuple[list[tuple[str, str | None]], list[tuple[str, str | None]]]:
    """Return ([(child sitemap URL, lastmod)], [(page URL, lastmod)]). Image/video sitemaps are skipped."""
    try:
        root = ET.fromstring(xml.strip().encode())
    except ET.ParseError:
        return [], []
    children, urls = [], []
    for sm in root.iter(f"{NS}sitemap"):
        loc = (sm.findtext(f"{NS}loc") or "").strip()
        name = loc.rsplit("/", 1)[-1]
        if loc and not name.startswith(("image-", "video-")):
            children.append((loc, (sm.findtext(f"{NS}lastmod") or "").strip() or None))
    for u in root.iter(f"{NS}url"):
        loc = (u.findtext(f"{NS}loc") or "").strip()
        if loc:
            urls.append((loc, (u.findtext(f"{NS}lastmod") or "").strip() or None))
    return children, urls
