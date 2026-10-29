from dataclasses import dataclass
from functools import cache

from fastembed import SparseTextEmbedding
from openai import OpenAI
from fastembed.rerank.cross_encoder import TextCrossEncoder
from qdrant_client import QdrantClient, models

from ole import pagetypes
from ole.chunk import chunk_id

import os
import re
import sys
import time
from datetime import date

import httpx

from ole.config import (RERANKER, VOYAGE_RERANK_MODEL, ANSWER_K, COLLECTION, DENSE_DIM, DENSE_MODEL, PER_URL_MAX, QDRANT_URL, RERANK_K, RERANK_MODEL,
                        RETRIEVE_K, SPARSE_MODEL)

EMBED_BATCH = 256


@dataclass
class Hit:
    id: str
    url: str
    title: str
    heading_path: str
    text: str
    lastmod: str | None
    score: float
    index: int = 0

    @property
    def embed_text(self) -> str:
        head = f"{self.title}\n{self.heading_path}" if self.heading_path else self.title
        return f"{head}\n\n{self.text}"


@cache
def openai_client() -> OpenAI:
    return OpenAI()


def embed_texts(texts: list[str]) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        r = openai_client().embeddings.create(model=DENSE_MODEL, input=texts[i:i + EMBED_BATCH], dimensions=DENSE_DIM)
        out += [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
    return out


@cache
def sparse_model() -> SparseTextEmbedding:
    return SparseTextEmbedding(SPARSE_MODEL)


@cache
def reranker() -> TextCrossEncoder:
    return TextCrossEncoder(RERANK_MODEL)


@cache
def http() -> httpx.Client:
    return httpx.Client(timeout=15)


FALLBACKS = [0]  # times Voyage failed and the local reranker stood in (the eval reports it)


def voyage_scores(query: str, docs: list[str]) -> list[float]:
    for wait in (1, 2, 0):  # bursts get 429s; a short wait beats the much weaker local reranker
        r = http().post("https://api.voyageai.com/v1/rerank",
                        headers={"Authorization": f"Bearer {os.environ['VOYAGE_API_KEY']}"},
                        json={"query": query, "documents": docs, "model": VOYAGE_RERANK_MODEL, "truncation": True})
        if r.status_code != 429 or not wait:
            break
        time.sleep(wait)
    r.raise_for_status()
    scores = [0.0] * len(docs)
    for d in r.json()["data"]:
        scores[d["index"]] = d["relevance_score"]
    return scores


def local_scores(query: str, docs: list[str]) -> list[float]:
    return [float(s) for s in reranker().rerank(query, docs)]


def rerank_scores(query: str, docs: list[str], which: str) -> list[float]:
    if which == "voyage":
        try:
            return voyage_scores(query, docs)
        except (httpx.HTTPError, KeyError) as e:  # Voyage down or no key: the local model still works
            FALLBACKS[0] += 1
            print(f"[search] voyage rerank failed ({type(e).__name__}), using local reranker", file=sys.stderr, flush=True)
    return local_scores(query, docs)


@cache
def qdrant() -> QdrantClient:
    return QdrantClient(QDRANT_URL)


def diversify(hits: list[Hit], per_url: int = PER_URL_MAX) -> list[Hit]:
    counts: dict[str, int] = {}
    out = []
    for h in hits:
        counts[h.url] = counts.get(h.url, 0) + 1
        if counts[h.url] <= per_url:
            out.append(h)
    return out


# Everyday questions ("when does stav hall close") should find the regular page, not a reunion schedule, conference
# housing, break hours or an old copy, which repeat the same words. Pages are labeled offline (ole.pagetypes). Only
# questions about dates, deadlines, hours, events or schedules are touched: the rewrite adds the current academic year
# to exactly those, so its "2026-27" is the intent flag. Then an occasion page drops unless the question names its
# occasion or any occasion, an old page unless it names a year or the past. Ungated, it cost answers on held-out.
TYPE_PENALTY = 0.2  # on Voyage's 0-1 scale (eval 2026-10-29); ponytail: small next to MiniLM fallback logits
OCCASION_WORDS = {"summer", "camp", "camps", "conference", "conferences", "reunion", "reunions", "homecoming", "alumni",
                  "interim", "break", "breaks", "guest", "guests", "archive", "archived", "old", "copy", "past", "thanksgiving",
                  "holiday", "holidays", "christmas", "winter", "family", "parents", "last", "previous", "j"}
PAST_WORDS = {"last", "previous", "past", "archive", "archived", "old", "former"}
GENERIC = {"the", "a", "of", "and", "for", "in", "s", "st", "olaf", "hours", "page", "info", "information", "students",
           "student", "college", "campus", "new", "week", "weekend", "weekends", "day", "class", "program", "fall", "spring"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def _stems(text: str) -> set[str]:
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in _words(text)} - GENERIC


def type_penalty(hit: Hit, queries: list[str], kinds: dict[str, tuple[str, str]], today: date | None = None) -> float:
    """queries: the rewrite, then the student's own words."""
    kind, about = pagetypes.page_kind(*kinds.get(hit.url, ("regular", "")), today)
    if pagetypes.year_tag(today) not in queries[0]:
        return 0.0
    q = " ".join(queries)
    if kind == "past":
        asked = _words(q) & PAST_WORDS or pagetypes.YEAR.search(queries[-1]) or \
            any(int(y) < pagetypes.academic_year(today) for y in pagetypes.YEAR.findall(q))
        return 0.0 if asked else TYPE_PENALTY
    if kind == "occasion":
        return 0.0 if _stems(q) & _stems(about) or _words(q) & OCCASION_WORDS else TYPE_PENALTY
    return 0.0


def search(query: str, k: int = ANSWER_K, raw: str | None = None) -> list[Hit]:
    """Hybrid search for the rewritten query and, if given, the student's own words (the rewrite
    sometimes drops the words the right page uses). The reranker sees both, natural question first."""
    queries = [query] + ([raw] if raw and raw.strip() != query.strip() else [])
    prefetch = []
    for q, dense in zip(queries, embed_texts(queries)):
        sparse = next(iter(sparse_model().query_embed(q)))
        prefetch += [models.Prefetch(query=dense, using="dense", limit=RETRIEVE_K),
                     models.Prefetch(query=models.SparseVector(indices=sparse.indices.tolist(), values=sparse.values.tolist()),
                                     using="bm25", limit=RETRIEVE_K)]
    res = qdrant().query_points(COLLECTION, prefetch=prefetch, query=models.FusionQuery(fusion=models.Fusion.RRF),
                                limit=RETRIEVE_K * len(queries), with_payload=True)
    hits = [Hit(id=str(p.id), score=p.score, **{f: p.payload.get(f) for f in
                ("url", "title", "heading_path", "text", "lastmod")}, index=p.payload.get("index", 0)) for p in res.points]
    question = "\n".join(reversed(queries))
    if RERANK_K and hits:  # 0 = trust the hybrid (RRF) order
        hits = hits[:RERANK_K]
        kinds = pagetypes.kinds(list({h.url for h in hits}))
        for h, s in zip(hits, rerank_scores(question, [h.embed_text for h in hits], RERANKER)):
            h.score = s - type_penalty(h, queries, kinds)
        hits.sort(key=lambda h: h.score, reverse=True)
    return expand(diversify(hits)[:k], question)


def merge_neighbors(hits: list[Hit], texts: dict[tuple[str, int], str], far: set[tuple[str, int]] = frozenset()) -> list[Hit]:
    """Glue each hit's previous and next chunk onto it (unless that chunk is a hit itself), and for the hits in `far`
    (url, index) the chunk after next too."""
    taken = {(h.url, h.index) for h in hits}
    for h in hits:
        after = (1, 2) if (h.url, h.index) in far else (1,)
        parts = [texts.get((h.url, h.index - 1)) if (h.url, h.index - 1) not in taken else None, h.text]
        for d in after:
            if (h.url, h.index + d) in taken or (h.url, h.index + d) not in texts:
                break
            parts.append(texts[(h.url, h.index + d)])
        h.text = "\n\n".join(p for p in parts if p)
    return hits


# A hit's answer can sit two chunks on: a page's "Stav Hall" card is one chunk, its "Dinner 4:30-8 p.m." column the
# chunk after next, with no "Stav Hall" in it. Gluing that far blindly brought in other buildings' schedules and the
# answers hedged, so Voyage reads the chunk after next under its hit's headings and it is glued only if relevant.
NEIGHBOR_MIN = 0.6  # Voyage relevance (0-1) the chunk after next needs (eval 2026-10-29)


def expand(hits: list[Hit], question: str | None = None) -> list[Hit]:
    ids = [chunk_id(h.url, i) for h in hits for i in (h.index - 1, h.index + 1, h.index + 2) if i >= 0]
    found = qdrant().retrieve(COLLECTION, ids, with_payload=True) if ids else []
    chunks = {(p.payload["url"], p.payload.get("index", 0)): p.payload for p in found}
    taken = {(h.url, h.index) for h in hits}
    near = [(h, chunks[(h.url, h.index + 2)]) for h in hits if (h.url, h.index + 2) in chunks
            and (h.url, h.index + 1) in chunks and not {(h.url, h.index + 1), (h.url, h.index + 2)} & taken]
    far = set()
    if question and near and RERANKER == "voyage":  # MiniLM scores aren't on a 0-1 scale
        docs = [f"{h.title}\n{h.heading_path}\n{c.get('heading_path') or ''}\n\n{c['text']}" for h, c in near]
        try:
            far = {(h.url, h.index) for (h, _), s in zip(near, voyage_scores(question, docs)) if s >= NEIGHBOR_MIN}
        except (httpx.HTTPError, KeyError) as e:  # the next and previous chunks still go in
            FALLBACKS[0] += 1
            print(f"[search] voyage neighbor scoring failed ({type(e).__name__})", file=sys.stderr, flush=True)
    return merge_neighbors(hits, {key: c["text"] for key, c in chunks.items()}, far)


def warmup() -> None:
    """Load the three local models so the first question isn't slow."""
    next(iter(sparse_model().query_embed("warm up")))
    if RERANK_K:
        list(reranker().rerank("warm up", ["warm up"]))  # also the fallback when Voyage is unreachable
