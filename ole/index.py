"""Extract, chunk and embed pages from data/pages.db into Qdrant.

Run: uv run python -m ole.index          (pages not embedded yet: hash is NULL)
     uv run python -m ole.index --full   (recreates the collection and re-embeds everything)
"""
import argparse
import hashlib
import json
import time

from qdrant_client import QdrantClient, models

from ole.chunk import Chunk, chunk_sections
from ole.config import COLLECTION, DENSE_DIM, QDRANT_URL, RAW_DIR
from ole.db import connect, lock, put
from ole.extract import extract_html, extract_pdf, extract_text
from ole.search import embed_texts, sparse_model

BATCH = 256


def ensure_collection(client: QdrantClient, recreate: bool = False) -> None:
    if recreate and client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            COLLECTION,
            vectors_config={"dense": models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE)},
            sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
    client.create_payload_index(COLLECTION, "url", models.PayloadSchemaType.KEYWORD)


def url_filter(urls: list[str], min_index: int = 0) -> models.Filter:
    return models.Filter(must=[models.FieldCondition(key="url", match=models.MatchAny(any=urls)),
                               models.FieldCondition(key="index", range=models.Range(gte=min_index))])


def delete_points(client: QdrantClient, urls: list[str]) -> None:
    client.delete(COLLECTION, url_filter(urls))


def extract_row(row: dict) -> tuple[str, list]:
    path = RAW_DIR / row["file"]
    if row["kind"] == "html":
        title, secs = extract_html(path.read_text(errors="replace"), row["url"])
    elif row["kind"] == "pdf":
        title, secs = extract_pdf(path.read_bytes(), row["url"])
    else:
        title, secs = extract_text(path.read_text(errors="replace"), row["title"] or "Document")
    return title or row["title"] or row["url"], secs


def page_chunks(row: dict) -> tuple[list[Chunk], str]:
    title, secs = extract_row(row)
    # lastmod is in the hash so the date the bot quotes stays current
    h = hashlib.sha256(json.dumps([title, row["lastmod"], secs]).encode()).hexdigest()
    return chunk_sections(row["url"], title, secs, row["lastmod"], row["kind"]), h


def upsert(client: QdrantClient, chunks: list[Chunk]) -> None:
    texts = [c.embed_text for c in chunks]
    client.upsert(COLLECTION, [
        models.PointStruct(
            id=c.id,
            vector={"dense": d, "bm25": models.SparseVector(indices=s.indices.tolist(), values=s.values.tolist())},
            payload={k: v for k, v in vars(c).items() if k != "id"},
        )
        for c, d, s in zip(chunks, embed_texts(texts), sparse_model().embed(texts, batch_size=64))
    ])


def index_pages(rows: list[dict], client: QdrantClient) -> list[str]:
    """The one write path into Qdrant. Each row is written back with its new hash only after its chunks are
    upserted and its leftover tail deleted, so a crash or a failed embedding leaves the old hash to retry.
    Returns the URLs whose content changed."""
    changed, pending, total, t0 = [], [], 0, time.monotonic()

    def flush():
        nonlocal total
        chunks = [c for _, cs in pending for c in cs]
        if chunks:
            upsert(client, chunks)
        with connect() as con:
            for row, cs in pending:
                client.delete(COLLECTION, url_filter([row["url"]], len(cs)))  # the page may have shrunk
                put(con, row)
        total += len(chunks)
        pending.clear()
        print(f"[index] {total} chunks ({total / (time.monotonic() - t0):.0f}/s)", flush=True)

    for row in rows:
        try:
            chunks, h = page_chunks(row)
        except Exception as e:
            print(f"[index] skip {row['url']}: {type(e).__name__}: {e}", flush=True)
            continue
        if h == row.get("hash"):
            with connect() as con:
                put(con, row)  # the caller's fetched/checked times still count
            continue
        changed.append(row["url"])
        pending.append(({**row, "hash": h}, chunks))
        if sum(len(cs) for _, cs in pending) >= BATCH:
            flush()
    if pending:
        flush()
    return changed


def build_index(client: QdrantClient, full: bool = False) -> int:
    ensure_collection(client, recreate=full)
    with connect() as con:
        if full:
            con.execute("UPDATE pages SET hash=NULL")
        rows = [dict(r) for r in con.execute("SELECT * FROM pages WHERE status='live' AND hash IS NULL ORDER BY url")]
    print(f"[index] {len(rows)} pages to embed", flush=True)
    index_pages(rows, client)
    return client.count(COLLECTION).count


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--full", action="store_true", help="recreate the collection (search is down until it finishes)")
    a = p.parse_args()
    with lock():
        print(f"[index] done: {build_index(QdrantClient(QDRANT_URL, timeout=120), a.full)} chunks in {COLLECTION}")
