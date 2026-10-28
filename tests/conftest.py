import numpy as np
import pytest
from qdrant_client import QdrantClient

import ole.db
import ole.index as index_mod
from ole.config import DENSE_DIM


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Every test gets its own pages.db and lock file, so none can touch data/."""
    monkeypatch.setattr(ole.db, "DB", tmp_path / "pages.db")
    monkeypatch.setattr(ole.db, "LOCK", tmp_path / "crawl.lock")


class FakeSparse:
    def embed(self, texts, batch_size=64):
        for t in texts:
            yield type("S", (), {"indices": np.array([len(t) % 97]), "values": np.array([1.0])})()


def fake_embeddings(monkeypatch, raw_dir, collection) -> list[int]:
    """Point ole.index at raw_dir and collection, with fake embeddings. Returns the embed batch sizes."""
    calls = []

    def embed(texts):
        calls.append(len(texts))
        return [[1.0] + [0.0] * (DENSE_DIM - 1) for _ in texts]

    monkeypatch.setattr(index_mod, "RAW_DIR", raw_dir)
    monkeypatch.setattr(index_mod, "COLLECTION", collection)
    monkeypatch.setattr(index_mod, "embed_texts", embed)
    monkeypatch.setattr(index_mod, "sparse_model", FakeSparse)
    return calls


@pytest.fixture
def qd(tmp_path, monkeypatch):
    """In-memory Qdrant and fake embeddings (no Docker, no OpenAI). Returns (client, embed batch sizes)."""
    calls = fake_embeddings(monkeypatch, tmp_path, "test")
    client = QdrantClient(":memory:")
    index_mod.ensure_collection(client)
    return client, calls
