import re
from io import BytesIO
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

import trafilatura
from bs4 import BeautifulSoup
from pypdf import PdfReader

from ole.urls import normalize


class Section(NamedTuple):
    heading_path: str
    text: str


SITE_SUFFIX = re.compile(r"\s*[<|–—-]\s*St\. Olaf College\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\xa0", " ")).strip()


def page_title(soup: BeautifulSoup) -> str:
    tag = soup.head.title if soup.head and soup.head.title else soup.title
    return SITE_SUFFIX.sub("", clean(tag.get_text())) if tag else ""


def extract_html(html: str, url: str) -> tuple[str, list[Section]]:
    soup = BeautifulSoup(html, "html.parser")
    title = page_title(soup)
    # accordion titles (FAQ questions, "2026-2027 Room Rates") are <button>s, which extraction drops as UI
    for b in soup.select("button.accordion__header"):
        b.name, b.attrs = "h4", {}
    sections = []
    if urlsplit(url).netloc == "catalog.stolaf.edu":
        for block in soup.select("div.courseblock"):
            head = block.select_one(".courseblocktitle")
            heading = clean(head.get_text(" ")).rstrip(".") if head else ""
            if head:
                head.decompose()
            text = clean(block.get_text(" "))
            if text:
                sections.append(Section(heading, text))
            block.decompose()
        # CourseLeaf puts each tab (overview, requirements, plan of study, faculty) in its own
        # container; trafilatura treats the hidden tabs as boilerplate, so extract each one alone.
        tabs = soup.select('div[id$="textcontainer"]')
        if tabs:
            for tab in tabs:
                sections += _markdown_sections(f"<html><body><main>{tab}</main></body></html>", [title])
            return title, sections
    return title, _markdown_sections(str(soup)) + sections


def _markdown_sections(html: str, prefix: list[str] = ()) -> list[Section]:
    md = trafilatura.extract(html, output_format="markdown", include_formatting=True, include_tables=True,
                             include_links=False, include_images=False, favor_recall=True) or ""
    sections, stack, lines = [], [], []

    def flush():
        text = "\n".join(lines).strip()
        if text:
            for entry in stack:
                entry[2] = True  # this heading has text under it
            path = [*prefix, *(h for _, h, _ in stack)]
            path = [p for i, p in enumerate(path) if i == 0 or p != path[i - 1]]
            sections.append(Section(" > ".join(path), clean_block(text)))
        lines.clear()

    for line in md.splitlines():
        m = HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            popped = []
            while stack and stack[-1][0] >= level:
                popped.append(stack.pop())
            # a heading with nothing under it is often a styled paragraph (WordPress h4/h5): keep its words, in place
            lines.extend(head for _, head, used in reversed(popped) if not used)
            flush()
            stack.append([level, clean(m.group(2).replace("*", "")), False])
        else:
            lines.append(line)
    flush()
    return sections


def clean_block(text: str) -> str:
    return "\n".join(clean(l) for l in text.splitlines() if l.strip())


def extract_pdf(data: bytes, url: str) -> tuple[str, list[Section]]:
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    try:
        reader = PdfReader(BytesIO(data))
        meta_title = (reader.metadata.title if reader.metadata else None) or ""
        pages = [clean_block(p.extract_text() or "") for p in reader.pages]
    except Exception:
        return name, []
    sections = [Section(f"Page {i}", t) for i, t in enumerate(pages, 1) if t]
    return clean(meta_title) or name, sections


def extract_text(text: str, title: str) -> tuple[str, list[Section]]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r", "")) if p.strip()]
    return title, [Section("", clean_block("\n\n".join(paras)))] if paras else []


def extract_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"].strip())
        if url.startswith(("http://", "https://")):
            url = normalize(url)
            if url not in out:
                out.append(url)
    return out
