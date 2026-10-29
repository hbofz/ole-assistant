import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB = DATA_DIR / "pages.db"
LOCK = DATA_DIR / "crawl.lock"  # one crawl, index or refresh at a time
MANIFEST = DATA_DIR / "manifest.jsonl"  # pre-pages.db format, read once by ole.db
GOOGLE_PENDING = DATA_DIR / "google_pending.jsonl"

USER_AGENT = "ole-assistant/0.1 (St. Olaf Piper Center research demo)"
CRAWL_DELAY = 4.0  # St. Olaf's firewall rate-limited us at 1 req/s
MAX_PAGES = 15000
ALLOWED_HOSTS = {"wp.stolaf.edu", "www.stolaf.edu", "catalog.stolaf.edu"}
SEED_SITEMAPS = ["https://wp.stolaf.edu/sitemap.xml", "https://catalog.stolaf.edu/sitemap.xml"]
SEED_PAGES = ["https://wp.stolaf.edu/", "https://wp.stolaf.edu/pipercenter/", "https://catalog.stolaf.edu/"]

QDRANT_URL = "http://localhost:6333"
COLLECTION = "stolaf"
DENSE_MODEL = "text-embedding-3-large"  # OpenAI; local CPU models were ~1 s per chunk
DENSE_DIM = 1024
SPARSE_MODEL = "Qdrant/bm25"
RERANKER = os.getenv("OLE_RERANKER", "voyage")  # "voyage" (rerank-3, hosted) or "local" (MiniLM on CPU)
VOYAGE_RERANK_MODEL = "rerank-3"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"  # bge-reranker-base took ~28 s per question on CPU

ANSWER_MODEL = os.getenv("OLE_MODEL", "claude-haiku-5-5")  # Hamzah: same eval scores as Sonnet, ~20x cheaper
GRADER_MODEL = "claude-sonnet-5-5"
REWRITE_MODEL = "claude-haiku-5-5"

RETRIEVE_K = 40
RERANK_K = int(os.getenv("OLE_RERANK_K", "20"))  # 0 turns the reranker off
ANSWER_K = 8
PER_URL_MAX = 3
LEFSE_PASSWORD = os.getenv("LEFSE_PASSWORD") or None  # set it to require a login (public link)
PROMPT_VERSION = "v5"  # v4: conflict notes, v5: no personal records
BOT_NAME = "Lefse"
