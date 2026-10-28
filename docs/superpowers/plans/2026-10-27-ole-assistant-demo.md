# ole-assistant Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local chatbot that answers any question about St. Olaf from its public websites, with numbered citations, plus an eval that proves it works.

**Architecture:** A polite sitemap-and-link crawler caches raw pages on disk. An indexer extracts sections, chunks them, embeds them locally (dense plus BM25 sparse) and loads them into Qdrant. At question time, hybrid search plus a local cross-encoder reranker picks the top chunks, which go to Claude as `search_result` blocks with citations enabled. The answer streams to a one-page web UI.

**Tech Stack:** Python 3.13 + uv, httpx, protego (robots.txt with wildcard support), trafilatura + beautifulsoup4, pypdf, fastembed (dense, sparse and rerank models on CPU), qdrant-client + Qdrant in Docker, anthropic SDK, FastAPI + uvicorn, pytest.

**Spec:** `docs/superpowers/specs/2026-10-27-ole-assistant-demo-design.md`

## Global Constraints

- No Opus. Answers use `claude-sonnet-5-5` by default (env `OLE_MODEL` overrides it, e.g. `claude-haiku-5-5`). Grading uses `claude-sonnet-5-5`. Follow-up rewriting uses `claude-haiku-5-5`.
- `ANTHROPIC_API_KEY` is read from `.env` (loaded with python-dotenv in `ole/config.py`). Never print, log or commit it.
- Obey robots.txt for every host. Wait at least 1.0 s between requests. User-Agent: `ole-assistant/0.1 (St. Olaf Piper Center research demo)`.
- Crawl only the hosts `wp.stolaf.edu`, `www.stolaf.edu` and `catalog.stolaf.edu`, plus Google Docs links found on those pages.
- Budget: dev plus eval spend under about $100 of the $200 credits. Any test that calls the Claude API is marked `@pytest.mark.live` and is excluded by default (`-m "not live"`).
- **Do not commit.** Hamzah confirms each verified milestone before anything is committed (his global rule). The "Checkpoint" steps below mean: tests pass, then report to Hamzah.
- No em dashes in any user-facing text (UI copy, system prompt, docs).
- Sonnet 5.5 requests: `client.beta.messages.stream(...)` with `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`, `output_config={"effort": "low"}`, and thinking omitted (adaptive by default). Haiku 5.5 requests: the same, but without `betas`/`fallbacks`. Before writing the call, check the citations and search-result shapes against the Claude API skill or live docs (`search_result` block, `citations_delta` stream event).

## Review Focus

1. **Follow-up questions** ("what about for sophomores?"). Expect a standalone query to be rewritten from the history before retrieval. Pinned in Task 6 (`test_rewrite_only_with_history`) and in the eval (Task 9 includes 3 follow-up pairs).
2. **Off-topic or unanswerable questions** ("what's the weather", "write my essay", facts the site doesn't have). Expect a polite decline with no citations, pointing to an office when relevant. Pinned in the eval set (Task 9: 8 unanswerable plus 2 off-topic), where `confident_wrong` must be 0.
3. **Stale pages.** Expect the answer to mention the date when a source is old. Pinned in Task 6 (`test_search_result_title_has_date`).
4. **robots.txt wildcard rules** like `/news/*?*`, which Python's stdlib `urllib.robotparser` silently ignores. Expect those URLs to be skipped. Pinned in Task 2 (`test_robots_wildcards`).
5. **An interrupted crawl** (Ctrl+C, network error, St. Olaf returning 5xx). Expect a re-run to resume without re-fetching cached pages, and one bad URL never to kill the crawl. Pinned in Task 3 (`test_crawl_resumes_and_survives_errors`).

---

## File Structure

```
compose.yaml            Qdrant container (port 6333, volume ./data/qdrant)
pyproject.toml          uv project, deps, pytest config (markers: live, integration)
ole/config.py           all constants + .env loading
ole/urls.py             normalize, noise filter, robots, sub-site sitemap guess
ole/sitemaps.py         sitemap XML parsing
ole/crawl.py            fetch loop, cache, manifest, Google link handling  (python -m ole.crawl)
ole/extract.py          HTML/PDF/text -> title + sections; link extraction
ole/chunk.py            sections -> Chunk
ole/index.py            embed + load Qdrant                                  (python -m ole.index)
ole/add_doc.py          register a manually fetched (Drive MCP) doc          (python -m ole.add_doc)
ole/search.py           hybrid search + rerank + per-URL diversity
ole/answer.py           Claude call: prompt, search_result blocks, streaming events
ole/web.py              FastAPI app (uv run uvicorn ole.web:app)
ole/static/index.html   chat UI
ole/evaluate.py         eval runner + grader                                 (python -m ole.evaluate)
eval/questions.jsonl    eval set
tests/                  one test file per module; tests/fixtures/ for HTML/XML samples
data/                   gitignored: raw/, manifest.jsonl, google_pending.jsonl, qdrant/, eval_cache/, eval_runs/
```

---

### Task 1: Project skeleton, config, Qdrant

**Files:**
- Create: `pyproject.toml`, `compose.yaml`, `ole/__init__.py`, `ole/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces (`ole/config.py`): `ROOT: Path`, `DATA_DIR = ROOT/"data"`, `RAW_DIR = DATA_DIR/"raw"`, `MANIFEST = DATA_DIR/"manifest.jsonl"`, `GOOGLE_PENDING = DATA_DIR/"google_pending.jsonl"`, `USER_AGENT`, `CRAWL_DELAY = 1.0`, `MAX_PAGES = 15000`, `ALLOWED_HOSTS = {"wp.stolaf.edu", "www.stolaf.edu", "catalog.stolaf.edu"}`, `SEED_SITEMAPS = ["https://wp.stolaf.edu/sitemap.xml", "https://catalog.stolaf.edu/sitemap.xml"]`, `SEED_PAGES = ["https://wp.stolaf.edu/", "https://wp.stolaf.edu/pipercenter/", "https://catalog.stolaf.edu/"]`, `QDRANT_URL = "http://localhost:6333"`, `COLLECTION = "stolaf"`, `DENSE_MODEL = "BAAI/bge-base-en-v1.5"`, `SPARSE_MODEL = "Qdrant/bm25"`, `RERANK_MODEL = "BAAI/bge-reranker-base"`, `ANSWER_MODEL = os.getenv("OLE_MODEL", "claude-sonnet-5-5")`, `GRADER_MODEL = "claude-sonnet-5-5"`, `REWRITE_MODEL = "claude-haiku-5-5"`, `RETRIEVE_K = 40`, `ANSWER_K = 8`, `PER_URL_MAX = 3`, `PROMPT_VERSION = "v1"`.

- [ ] **Step 1:** `uv init --package`-style layout by hand (package `ole`), Python `>=3.13`. Add deps: `httpx protego trafilatura beautifulsoup4 pypdf fastembed qdrant-client anthropic fastapi uvicorn python-dotenv`. Dev: `pytest`. In `[tool.pytest.ini_options]` register the markers `live` and `integration`, with `addopts = "-m 'not live'"`.
- [ ] **Step 2:** `compose.yaml` with a single `qdrant` service: image `qdrant/qdrant` pinned to the current stable tag (look it up, don't use `latest`), ports `6333:6333`, volume `./data/qdrant:/qdrant/storage`.
- [ ] **Step 3: Write test** `tests/test_config.py::test_env_loaded`: assert `os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-")` after `import ole.config`.
- [ ] **Step 4:** Implement `ole/config.py` (`load_dotenv(ROOT/".env")` first).
- [ ] **Step 5: Verify.** Run `uv sync && uv run pytest -q` and expect a pass. Run `docker compose up -d && curl -s localhost:6333/readyz` and expect `all shards are ready`.

### Task 2: URL rules and sitemap parsing

**Files:**
- Create: `ole/urls.py`, `ole/sitemaps.py`, `tests/test_urls.py`, `tests/test_sitemaps.py`, `tests/fixtures/robots_wp.txt` (copy of the live wp.stolaf.edu robots.txt), `tests/fixtures/sitemap_index.xml`, `tests/fixtures/sitemap_urlset.xml`

**Interfaces:**
- Produces: `normalize(url: str) -> str` (https, lowercase host, no fragment, `www.stolaf.edu` paths mapped to the same URL on `wp.stolaf.edu` only if that host redirects there; otherwise unchanged). `is_noise(url: str) -> bool`. `class Robots: def __init__(self, client: httpx.Client); def allowed(self, url: str) -> bool` (protego, one fetch per host, cached; an unreachable robots.txt means allow, and a 5xx means disallow). `subsite_sitemap(url: str) -> str | None`. `parse_sitemap(xml: str) -> tuple[list[str], list[tuple[str, str | None]]]` returns (child sitemaps, (url, lastmod) pairs) and skips `image-`/`video-` sitemaps.

- [ ] **Step 1: Write failing tests:**

```python
def test_noise():
    assert is_noise("https://wp.stolaf.edu/news/some-story/")
    assert is_noise("https://wp.stolaf.edu/calendar/event/x/")
    assert is_noise("https://wp.stolaf.edu/pipercenter/?s=intern")
    assert is_noise("https://wp.stolaf.edu/tag/alumni/")
    assert is_noise("https://wp.stolaf.edu/registrar/2019/05/")
    assert not is_noise("https://wp.stolaf.edu/pipercenter/internships/")
    assert not is_noise("https://catalog.stolaf.edu/academic-programs/computer-science/")

def test_robots_wildcards():  # Robots fed tests/fixtures/robots_wp.txt via a fake client
    assert not r.allowed("https://wp.stolaf.edu/news/a?b=1")
    assert not r.allowed("https://wp.stolaf.edu/catalog/x/")
    assert r.allowed("https://wp.stolaf.edu/pipercenter/")

def test_subsite_sitemap():
    assert subsite_sitemap("https://wp.stolaf.edu/registrar/forms/") == "https://wp.stolaf.edu/registrar/sitemap.xml"
    assert subsite_sitemap("https://catalog.stolaf.edu/x/") is None

def test_parse_sitemap_index_skips_media(): ...   # index fixture -> only sitemap-N.xml children
def test_parse_sitemap_urlset_lastmod(): ...      # urlset fixture -> [(url, "2026-..."), ...]
```

- [ ] **Step 2:** Run `uv run pytest tests/test_urls.py tests/test_sitemaps.py` and expect a FAIL (import error).
- [ ] **Step 3:** Implement. `is_noise` is a list of path regexes (`/news/`, `/news-archive/`, `/calendar/`, `/events?/`, `/tag/`, `/category/`, `/author/`, `/page/\d+/`, `/\d{4}/\d{2}/`, `/feed/`, `/wp-json/`, `wp-login`) plus "any query string". Extend the list after looking at the real sitemap contents.
- [ ] **Step 4:** Run the same tests and expect a PASS.
- [ ] **Step 5: Checkpoint.**

### Task 3: Extraction and the crawler

**Files:**
- Create: `ole/extract.py`, `ole/crawl.py`, `tests/test_extract.py`, `tests/test_crawl.py`, `tests/fixtures/catalog_cs.html` (saved from catalog.stolaf.edu/academic-programs/computer-science/), `tests/fixtures/wp_page.html` (saved from wp.stolaf.edu/pipercenter/)

**Interfaces:**
- Produces (`extract.py`): `Section = NamedTuple("Section", [("heading_path", str), ("text", str)])`. `extract_html(html: str, url: str) -> tuple[str, list[Section]]`. `extract_pdf(data: bytes, url: str) -> tuple[str, list[Section]]` (one section per page, `heading_path = f"Page {n}"`, and `[]` for scanned/empty PDFs). `extract_text(text: str, title: str) -> tuple[str, list[Section]]`. `extract_links(html: str, base_url: str) -> list[str]` (absolute, normalized, http(s) only).
- Produces (`crawl.py`): manifest rows (JSON lines in `MANIFEST`): `{"url", "file", "kind": "html"|"pdf"|"text", "lastmod", "source": "crawl"|"gdoc_export"|"drive_mcp", "title"}`. Google links that can't be exported are written to `GOOGLE_PENDING` as `{"url", "found_on"}`. `crawl(client: httpx.Client, max_pages: int = MAX_PAGES) -> None`. CLI: `uv run python -m ole.crawl`.

- [ ] **Step 1: Write failing tests:**

```python
def test_catalog_courseblocks():
    title, secs = extract_html(read("catalog_cs.html"), "https://catalog.stolaf.edu/academic-programs/computer-science/")
    course = [s for s in secs if "CSCI 251" in s.heading_path]
    assert len(course) == 1 and len(course[0].text) > 50

def test_wp_heading_paths():
    title, secs = extract_html(read("wp_page.html"), "https://wp.stolaf.edu/pipercenter/")
    assert "Piper" in title and all(s.text.strip() for s in secs)

def test_extract_links_and_google():
    links = extract_links('<a href="/registrar/">r</a><a href="mailto:x@y">m</a>'
                          '<a href="https://docs.google.com/document/d/ABC/edit">d</a>', "https://wp.stolaf.edu/a/")
    assert "https://wp.stolaf.edu/registrar/" in links and not any(l.startswith("mailto") for l in links)
    assert any("docs.google.com/document/d/ABC" in l for l in links)

def test_crawl_resumes_and_survives_errors(tmp_path, monkeypatch):
    # httpx.MockTransport: sitemap lists /a/ /b/ /c/; /b/ returns 500.
    # Run 1: manifest has /a/ and /c/, no crash. Run 2: zero requests for /a/ and /c/.
```

- [ ] **Step 2:** Run `uv run pytest tests/test_extract.py tests/test_crawl.py` and expect a FAIL.
- [ ] **Step 3:** Implement `extract.py`. On catalog.stolaf.edu, each `div.courseblock` becomes its own Section (heading = the `.courseblocktitle` text), and the rest of the page goes through the generic path. Generic path: trafilatura `extract(..., output_format="markdown", include_links=False, include_tables=True)`, split on markdown headings while keeping a heading stack to build `heading_path` (`"Internships > Funding"`). Title comes from `<title>` with the trailing ` – St. Olaf College` removed.
- [ ] **Step 4:** Implement `crawl.py`. The queue starts with the URLs from `SEED_SITEMAPS` (recursing into child sitemaps) plus `SEED_PAGES`. For every newly seen first path segment on wp.stolaf.edu, also fetch `subsite_sitemap(url)` (a 404 is fine). Each fetched HTML page adds its in-scope links to the queue. Skip `is_noise`, robots-disallowed and already-in-manifest URLs. PDFs are fetched only when linked from in-scope pages and under 20 MB. For Google Docs links `docs.google.com/document/d/<id>`, try `https://docs.google.com/document/d/<id>/export?format=txt`: if the final URL is on `accounts.google.com` or the content type is HTML, it goes to `GOOGLE_PENDING`. All other Google links (Sheets, Slides, Drive files) go to `GOOGLE_PENDING` too. Files are saved as `RAW_DIR/<sha1(url)>.<ext>`, and the manifest row is appended right after each save, so resuming works. Catch per-URL exceptions, log one line, continue. Print progress every 50 pages.
- [ ] **Step 5:** Run the tests and expect a PASS.
- [ ] **Step 6: Real crawl.** Run `uv run python -m ole.crawl`. Expect a few thousand manifest rows and zero robots violations. Spot-check that the manifest contains `wp.stolaf.edu/pipercenter/` pages, the registrar, and catalog course pages. Look at which first segments dominate and extend `is_noise` if junk shows up.
- [ ] **Step 7: Checkpoint.** Report the page count by host and kind to Hamzah.

### Task 4: Google Docs via the Drive MCP (done by the Claude Code session, not code)

**Files:**
- Create: `ole/add_doc.py`, `tests/test_add_doc.py`

**Interfaces:**
- Produces: `add_doc(url: str, title: str, text_file: Path) -> None` copies the file into `RAW_DIR` and appends a manifest row `kind="text", source="drive_mcp"`. Calling it twice with the same URL replaces the old row instead of duplicating it. CLI: `uv run python -m ole.add_doc --url U --title T --file F`.

- [ ] **Step 1: Write a failing test** `test_add_doc_idempotent`: calling `add_doc` twice with the same URL leaves exactly one manifest row for it.
- [ ] **Step 2:** Implement and expect a PASS.
- [ ] **Step 3:** For each row in `GOOGLE_PENDING`: get the file ID, call Drive MCP `get_file_permissions`. If anyone (or the `stolaf.edu` domain) has access, call `read_file_content` and save it to the scratchpad as `.txt`, then run `ole.add_doc`. Otherwise skip it. Keep a skip list with the reason for each.
- [ ] **Step 4: Checkpoint.** Report counts: exported publicly, added via MCP, skipped (by reason).

### Task 5: Chunking, indexing, search

**Files:**
- Create: `ole/chunk.py`, `ole/index.py`, `ole/search.py`, `tests/test_chunk.py`, `tests/test_search.py`

**Interfaces:**
- Consumes: `extract_html` / `extract_pdf` / `extract_text`, `MANIFEST`.
- Produces (`chunk.py`): `@dataclass Chunk(id: str, url: str, title: str, heading_path: str, text: str, lastmod: str | None, kind: str)` with the property `embed_text -> str` (`f"{title}\n{heading_path}\n\n{text}"`). `chunk_sections(url, title, sections, lastmod, kind, max_words=300, overlap_words=50) -> list[Chunk]`. IDs are `str(uuid5(NAMESPACE_URL, f"{url}#{i}"))`.
- Produces (`index.py`): `build_index() -> int` (recreates `COLLECTION`: named dense vector `"dense"` (cosine, size from the model) and sparse vector `"bm25"` with `modifier=IDF`; payload = every Chunk field). CLI: `uv run python -m ole.index`.
- Produces (`search.py`): `@dataclass Hit(id, url, title, heading_path, text, lastmod, score: float)`. `search(query: str, k: int = ANSWER_K) -> list[Hit]`. `diversify(hits: list[Hit], per_url: int = PER_URL_MAX) -> list[Hit]` (keeps the order, drops a hit once its URL already has `per_url` hits).

- [ ] **Step 1: Write failing tests:**

```python
def test_split_with_overlap():      # 700-word section -> 3 chunks, consecutive chunks share 50 words
def test_short_sections_merged():   # sections under 40 words merge into the next one under the same parent heading
def test_ids_deterministic():       # same input twice -> same ids
def test_embed_text_has_context():  # title and heading_path both appear in embed_text
def test_diversify():               # 5 hits from url A + 1 from B, per_url=3 -> A,A,A,B

@pytest.mark.integration
def test_course_code_exact():       # search("CSCI 251") top 3 includes a catalog.stolaf.edu computer-science URL
@pytest.mark.integration
def test_piper_internships():       # search("how do I find an internship") top 5 includes a /pipercenter/ URL
```

- [ ] **Step 2:** Run `uv run pytest tests/test_chunk.py tests/test_search.py -m "not integration"` and expect a FAIL.
- [ ] **Step 3:** Implement `chunk.py` and `diversify`. Expect a PASS.
- [ ] **Step 4:** Implement `index.py` (fastembed `TextEmbedding(DENSE_MODEL)` and `SparseTextEmbedding(SPARSE_MODEL)`, batches of 64, upsert batches of 256, progress output). Implement `search()`: `query_points` with two `Prefetch` (dense and sparse, `limit=RETRIEVE_K` each), `FusionQuery(fusion=Fusion.RRF)`, `limit=RETRIEVE_K`, then `TextCrossEncoder(RERANK_MODEL)` rescoring of `embed_text`, sort, `diversify`, then take the top `k`. Load the models once at module level, lazily.
- [ ] **Step 5:** Run `uv run python -m ole.index`. Expect a chunk count printed with no errors.
- [ ] **Step 6:** Run `uv run pytest -m integration` and expect a PASS.
- [ ] **Step 7: Checkpoint.**

### Task 6: Answering with Claude

**Files:**
- Create: `ole/answer.py`, `tests/test_answer.py`

**Interfaces:**
- Consumes: `search`, `Hit`, config models.
- Produces: `SYSTEM_PROMPT: str`. `rewrite_query(history: list[dict], question: str, client) -> str` (returns `question` unchanged and makes no API call when `history` is empty; otherwise one `REWRITE_MODEL` call that returns a standalone question). `build_messages(history: list[dict], question: str, hits: list[Hit]) -> list[dict]`, where `history` items are `{"role": "user"|"assistant", "content": str}` (plain text only). `stream_answer(history: list[dict], question: str, model: str = ANSWER_MODEL) -> Iterator[dict]`, which yields events: `{"type": "text", "text"}`, `{"type": "cite", "n": int}` (placed right after the cited text), `{"type": "sources", "items": [{"n", "url", "title"}]}`, `{"type": "done"}`, or `{"type": "error", "message"}`. `answer_text(history, question, model) -> tuple[str, list[dict], list[str]]` (full text with `[n]` markers, sources, retrieved hit IDs; used by eval).

- [ ] **Step 1: Write failing tests:**

```python
def test_build_messages_shape():
    msgs = build_messages([], "q?", [hit(url="https://wp.stolaf.edu/x/", lastmod="2021-03-01")])
    last = msgs[-1]["content"]
    assert last[0]["type"] == "search_result" and last[0]["citations"] == {"enabled": True}
    assert last[0]["source"] == "https://wp.stolaf.edu/x/" and last[-1] == {"type": "text", "text": "q?"}

def test_search_result_title_has_date():
    assert "2021-03-01" in build_messages([], "q", [hit(lastmod="2021-03-01")])[-1]["content"][0]["title"]

def test_rewrite_only_with_history():
    fake = FakeClient()
    assert rewrite_query([], "q", fake) == "q" and fake.calls == 0

def test_citation_numbering():  # citations to urls B, A, B -> n = 1, 2, 1; sources list in first-cited order

@pytest.mark.live
def test_live_smoke():          # "Where is the Piper Center?" -> non-empty text, at least 1 source on wp.stolaf.edu
```

- [ ] **Step 2:** Run `uv run pytest tests/test_answer.py` and expect a FAIL.
- [ ] **Step 3:** Implement. The `search_result` title is `f"{title} | {heading_path} (updated {lastmod})"`, leaving out the parts that are missing. `SYSTEM_PROMPT` (exact intent, wording can be polished):

```
You are Ole Assistant, a helper for St. Olaf College students. Answer using only the search results provided with each question, and cite them. If the results don't contain the answer, say you couldn't find it on St. Olaf's public pages, and point the student to the office or page most likely to know. Never guess at policies, dates, deadlines or requirements. If a source looks old (its updated date is more than about two years ago), mention that the student should confirm it. Politely decline requests unrelated to St. Olaf. Keep answers short and scannable: lead with the direct answer, then details. Do not use em dashes.
```

  Streaming: number sources by URL in first-cited order. Map `citations_delta` to `cite` events. On `stop_reason == "refusal"`, yield `error` with "Sorry, I can't help with that one." Wrap API errors (`anthropic.APIStatusError`, `APIConnectionError`) as `error` events with no traceback text.
- [ ] **Step 4:** Run `uv run pytest tests/test_answer.py` (expect PASS), then `uv run pytest -m live tests/test_answer.py` (expect PASS, cost about $0.03).
- [ ] **Step 5: Checkpoint.**

### Task 7: Web UI

**Files:**
- Create: `ole/web.py`, `ole/static/index.html`, `tests/test_web.py`

**Interfaces:**
- Consumes: `stream_answer`.
- Produces: `GET /` serves `index.html`. `POST /api/chat` with body `{"history": [...], "question": str}` returns `text/event-stream`, one `data: <json event>\n\n` per event. Empty or over-2000-character questions get a 400.

- [ ] **Step 1: Write failing tests** (`TestClient`, `stream_answer` monkeypatched to yield text, cite, sources, done): the response streams those 4 events in order, and `{"question": ""}` returns 400.
- [ ] **Step 2:** Implement and expect a PASS.
- [ ] **Step 3:** `index.html`, a single file with inline CSS/JS. Title "Ole Assistant (demo)", no St. Olaf logos. It has a chat column, 4 starter questions (courses, Piper Center, registrar, internships), and streaming text rendered as light markdown (bold, lists, links). `cite` events become superscript `[n]` links, and a "Sources" list goes under each answer. History is kept in memory for follow-ups, with a "New chat" button. It works on phone width and in dark mode.
- [ ] **Step 4: Verify in a browser.** Run `uv run uvicorn ole.web:app --port 8000`, then ask the starter questions plus one follow-up. Each answer must stream, every `[n]` must open the right page, and the follow-up must use context. Screenshot it for Hamzah.
- [ ] **Step 5: Checkpoint.**

### Task 8: Eval runner

**Files:**
- Create: `ole/evaluate.py`, `tests/test_evaluate.py`

**Interfaces:**
- Consumes: `search`, `answer_text`.
- Produces: question rows `{"id", "area", "question", "expected_answer", "expected_urls": [...], "answerable": bool, "history": [...] (optional, for follow-ups)}`. `grade(row, answer: str) -> dict` returns `{"correct": bool, "declined": bool, "reason": str}` via `GRADER_MODEL` with `output_config.format` JSON schema. `summarize(results: list[dict]) -> dict` returns `{"retrieval_hit", "correct", "confident_wrong", "passed"}`. CLI: `uv run python -m ole.evaluate [--model M] [--only ID]` prints a per-area table and the summary, and writes `data/eval_runs/<timestamp>-<model>.json`. Answers are cached in `data/eval_cache/<sha256(model, question, history, hit ids, PROMPT_VERSION)>.json`.

- [ ] **Step 1: Write failing tests** for `summarize`: retrieval_hit and correct are computed over answerable rows only, `confident_wrong` counts unanswerable rows where `declined` is false, and `passed` is true only when retrieval_hit >= 0.90, correct >= 0.85 and confident_wrong == 0. Edge case: a set with only unanswerable rows doesn't divide by zero.
- [ ] **Step 2:** Implement and expect a PASS.
- [ ] **Step 3: Checkpoint.**

### Task 9: Eval set, run, tune

**Files:**
- Create: `eval/questions.jsonl`

- [ ] **Step 1:** Draft 52 questions from the crawled content. 40 answerable, spread across courses/catalog, majors and requirements, registrar and credits, the Piper Center, internships, financial aid, residence life, and other centers/offices. 3 of those are follow-up pairs that use `history`. Add 8 unanswerable but plausible questions (St. Olaf topics the site doesn't cover) and 2 off-topic ones. Every answerable row's `expected_urls` must be pages that actually exist in the manifest.
- [ ] **Step 2: Hamzah reviews the set.** Stop here until he has.
- [ ] **Step 3:** Run `uv run python -m ole.evaluate` (Sonnet 5.5, about $2 to $4). Report the summary.
- [ ] **Step 4:** If it didn't pass, read the failures, fix the actual cause (noise filter, extraction, chunk size, `RETRIEVE_K`/`ANSWER_K`, prompt wording; bump `PROMPT_VERSION` when the prompt changes), and re-run. Track cumulative spend from `usage`, and stop to report if eval spend passes $60.
- [ ] **Step 5:** Run with `--model claude-haiku-5-5`. Report Sonnet against Haiku: scores and cost per question.
- [ ] **Step 6: Checkpoint.** Show the final eval table and a demo run. On Hamzah's OK, `git init` and commit.
