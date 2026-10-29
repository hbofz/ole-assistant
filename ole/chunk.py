import re
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from ole.extract import Section

MIN_WORDS = 40


@dataclass
class Chunk:
    id: str
    url: str
    title: str
    heading_path: str
    text: str
    lastmod: str | None
    kind: str
    index: int = 0

    @property
    def embed_text(self) -> str:
        head = f"{self.title}\n{self.heading_path}" if self.heading_path else self.title
        return f"{head}\n\n{self.text}"


def _tokens(text: str) -> list[str]:
    return re.findall(r"\S+\s*", text)  # word + trailing whitespace, so newlines survive


def chunk_sections(url: str, title: str, sections: list[Section], lastmod: str | None, kind: str,
                   max_words: int = 300, overlap_words: int = 50) -> list[Chunk]:
    # fold short sections into the next one so tiny fragments don't become chunks; each folded
    # section's heading goes on the line right above its own text
    merged: list[Section] = []
    carry = ""
    for i, s in enumerate(sections):
        label = s.heading_path.rsplit(" > ", 1)[-1]
        own = f"{label}\n{s.text}" if label else s.text
        text = f"{carry}{own}" if carry else s.text
        if len(text.split()) < MIN_WORDS and i < len(sections) - 1:
            carry = f"{carry}{own}\n\n"
            continue
        carry = ""
        merged.append(Section(s.heading_path, text))

    texts: list[tuple[str, str]] = []
    step = max_words - overlap_words
    for s in merged:
        toks = _tokens(s.text)
        start = 0
        while True:
            texts.append((s.heading_path, "".join(toks[start:start + max_words]).strip()))
            if start + max_words >= len(toks):
                break
            start += step
    texts = [(h, t) for h, t in texts if t]
    return [Chunk(chunk_id(url, i), url, title, h, t, lastmod, kind, i) for i, (h, t) in enumerate(texts)]


def chunk_id(url: str, index: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"{url}#{index}"))
