# Lefse

A chatbot that answers St. Olaf College students' questions from St. Olaf's public websites, with numbered citations that link to the page each fact came from. It's a proof-of-concept demo for the Piper Center AI research team.

## How it works

1. **Crawl** (`ole/crawl.py`): reads the sitemaps of wp.stolaf.edu (main site and office sub-sites) and catalog.stolaf.edu, follows in-scope links, obeys robots.txt and backs off when the site rate-limits it. Raw pages are cached in `data/raw/`, with one row per page in `data/pages.db` (SQLite: URL, file, lastmod, when it was fetched and last checked, whether it's live or gone, and a hash of the text Qdrant holds for it). Public Google Docs linked from pages are exported as text. Docs that need a St. Olaf login are listed in `data/google_pending.jsonl`.
2. **Index** (`ole/index.py`): extracts text by section (course catalog entries become one section per course), splits it into ~300-word chunks, and stores them in Qdrant with an OpenAI `text-embedding-3-large` vector and a BM25 keyword vector. It only embeds pages whose hash is empty (new or re-fetched), and replaces a page's chunks in place, so search stays up. `--full` rebuilds the collection from scratch.
3. **Refresh** (`ole/refresh.py`): keeps the index current; see below.
4. **Search** (`ole/search.py`): runs keyword and semantic search together (merged with reciprocal rank fusion), reranks the top 20 with a small local cross-encoder, keeps at most 3 chunks per page, and attaches each hit's neighboring chunks.
5. **Answer** (`ole/answer.py`): Claude Haiku 5.5 answers only from the retrieved chunks, which are passed as `search_result` blocks so citations point at real sources. Before searching, Claude Haiku 5.5 rewrites every question into a short keyword query, using a small glossary of St. Olaf terms ("gen eds" becomes "OLE Core") and the conversation, so follow-ups like "what about the minor?" work.
6. **Chat page** (`ole/web.py`, `ole/static/`): streams the answer, shows the sources under it, and says when the knowledge was last refreshed (`GET /api/status`).

## Run it

Needs Docker, [uv](https://docs.astral.sh/uv/), and a `.env` file in the project root with:

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
```

```bash
uv sync
docker compose up -d                 # Qdrant on localhost:6333 (local only, no auth)
uv run python -m ole.crawl           # hours: St. Olaf rate-limits crawlers; safe to stop and re-run
uv run python -m ole.index           # embeds pages not yet indexed; --full rebuilds (search is down meanwhile)
uv run uvicorn ole.web:app --port 8000
```

Then open http://localhost:8000. To share it, set `LEFSE_PASSWORD=...` in `.env` (visitors then get a login page) and expose the port over HTTPS, for example `tailscale funnel --bg --https=8443 http://127.0.0.1:8000` (turn off with `tailscale funnel --https=8443 off`). Add `--host <address>` to reach it from another device (for example a Tailscale IP).

To add a document by hand (for example a login-only Google Doc read through the Drive MCP): `uv run python -m ole.add_doc --url URL --title TITLE --file text.txt`, then re-run the index.

Crawl, index and refresh share a lock (`data/crawl.lock`): if one is running, the others say so and exit 1. Upgrading from the old `data/manifest.jsonl`: run `uv run python -m ole.db` once (it imports the manifest, keeps it as `manifest.jsonl.bak`), then `uv run python -m ole.index`.

## Keeping it current

`uv run python -m ole.refresh` brings the index up to date, cheaply enough to run nightly (about 40 to 60 minutes at St. Olaf's rate limit, a fraction of a cent of embeddings, no Claude calls):

- Reads the sitemaps and fetches pages that are new or whose `lastmod` changed (at most 300 new pages per run, `--new`). Sub-sitemaps whose `lastmod` hasn't moved are skipped.
- Re-fetches the 300 pages fetched longest ago (`--checks`), which catches what `lastmod` misses (widgets, PDFs, public Google Docs), so every page is re-checked every few weeks.
- Re-embeds only pages whose text changed. A 404/410 or a redirect marks a page gone and removes it from search. If St. Olaf's firewall blocks it (Retry-After of 5 minutes or more), it stops with exit 1 and the next run carries on.
- Prints every added, changed and gone URL, then a summary line.

`ole.refresh` doesn't follow links to discover new link-only pages; re-run `ole.crawl` now and then for that (it only fetches pages it hasn't seen). Drive-MCP docs never refresh on their own.

Suggested schedule (not installed; add it with `crontab -e` if you want it):

```
0 3 * * * cd /home/bb/projects/ole-assistant && /home/bb/.local/bin/uv run python -m ole.refresh >> data/refresh.log 2>&1
```

`.env` is loaded by `ole.config`, so cron needs no extra environment.

## Test it

```bash
uv run pytest                        # fast tests, no API calls
uv run pytest -m integration         # needs Qdrant and a built index
uv run pytest -m live                # calls Claude (costs a few cents)
uv run python -m ole.evaluate        # scores Lefse on eval/questions.jsonl (about $2 to $4 per run)
uv run python -m ole.evaluate --model claude-haiku-5-5
```

The eval passes when retrieval finds the right page for at least 90% of answerable questions, at least 85% of answers are graded correct, and Lefse gives zero confident answers to questions the site can't answer.

## Notes for a real deployment?

- Schedule `ole.refresh` (above).
- Docs fetched through the Drive MCP don't refresh on their own.
