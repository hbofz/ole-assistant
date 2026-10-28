"""The refresh scenario from test_refresh.py against a throwaway collection on the real Qdrant server."""
import os

import pytest
from qdrant_client import QdrantClient

import ole.index as index_mod
from ole.config import QDRANT_URL
from tests.conftest import fake_embeddings
from tests.test_refresh import test_refresh, world  # noqa: F401  (world is a fixture this test needs)

test_refresh = pytest.mark.integration(test_refresh)


@pytest.fixture
def qd(tmp_path, monkeypatch):
    name = f"test_refresh_{os.getpid()}"
    calls = fake_embeddings(monkeypatch, tmp_path, name)
    client = QdrantClient(QDRANT_URL)
    index_mod.ensure_collection(client)
    index_mod.ensure_collection(client)  # the url payload index is created idempotently on a real server
    yield client, calls
    client.delete_collection(name)
