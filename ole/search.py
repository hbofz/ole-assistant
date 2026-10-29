from dataclasses import dataclass
from functools import cache

from fastembed import SparseTextEmbedding
from openai import OpenAI
from fastembed.rerank.cross_encoder import TextCrossEncoder
from qdrant_client import QdrantClient, models

from ole.chunk import chunk_id

import os
import sys
import time

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
    if RERANK_K and hits:  # 0 = trust the hybrid (RRF) order
        hits = hits[:RERANK_K]
        for h, s in zip(hits, rerank_scores("\n".join(reversed(queries)), [h.embed_text for h in hits], RERANKER)):
            h.score = s
        hits.sort(key=lambda h: h.score, reverse=True)
    return expand(diversify(hits)[:k])


def merge_neighbors(hits: list[Hit], texts: dict[tuple[str, int], str]) -> list[Hit]:
    """Glue each hit's previous and next chunk onto it (unless that chunk is a hit itself)."""
    taken = {(h.url, h.index) for h in hits}
    for h in hits:
        parts = [texts.get((h.url, h.index - 1)) if (h.url, h.index - 1) not in taken else None, h.text,
                 texts.get((h.url, h.index + 1)) if (h.url, h.index + 1) not in taken else None]
        h.text = "\n\n".join(p for p in parts if p)
    return hits


def expand(hits: list[Hit]) -> list[Hit]:
    ids = [chunk_id(h.url, i) for h in hits for i in (h.index - 1, h.index + 1) if i >= 0]
    found = qdrant().retrieve(COLLECTION, ids, with_payload=True) if ids else []
    texts = {(p.payload["url"], p.payload.get("index", 0)): p.payload["text"] for p in found}
    return merge_neighbors(hits, texts)


def warmup() -> None:
    """Load the three local models so the first question isn't slow."""
    next(iter(sparse_model().query_embed("warm up")))
    if RERANK_K:
        list(reranker().rerank("warm up", ["warm up"]))  # also the fallback when Voyage is unreachable
