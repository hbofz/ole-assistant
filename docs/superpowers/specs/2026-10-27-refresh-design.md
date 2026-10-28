# Lefse refresh: keeping the knowledge current

Date: 2026-10-27. Extends `2026-10-27-ole-assistant-demo-design.md`. Hamzah approved the approach and asked for a design review before building.

## Why

St. Olaf's pages change (deadlines, policies, staff, programs). A campus bot that answers from stale pages is worse than none. Today the only way to update is a full re-crawl (about 7,000 pages at St. Olaf's firewall limit of one request every ~4 s, so many hours) plus a full re-index, which wipes the Qdrant collection and leaves the bot down for about a minute.

## Goals

- A single command, `uv run python -m ole.refresh`, that brings the index up to date with St. Olaf's sites, cheaply enough to run nightly.
- Only re-fetch what probably changed. Only re-embed what actually changed. Remove pages that no longer exist.
- No search downtime during a refresh.
- A record of every page's freshness (when it was last modified, fetched and checked) and of every refresh run.
- The chat page shows when the knowledge was last refreshed.

## Non-goals

- Real-time change watching (St. Olaf publishes no change feed; nightly is enough for policies and deadlines).
- Refreshing Drive-MCP-imported docs automatically (they stay a manual step, flagged as such).
- Installing the nightly schedule. That's persistent system config outside the repo, so we document the cron/systemd line and Hamzah decides.

## Design (revised after design review; this section supersedes the first draft)

Review findings from the real data: Jetpack sitemap lastmod tracks WordPress post_modified and can be trusted for editor edits, but it misses widget/shortcode changes. HTTP ETag/Last-Modified come from the page cache ("modified today", max-age=3), so conditional GETs are useless and get cut. Global chunk-text dedupe removes 11% of chunks (catalog program pages repeating course blocks) and can't be maintained incrementally.

1. **Schema.** `data/pages.db` (stdlib sqlite3, WAL). One table `pages(url PK, file, kind, source, title, lastmod, hash, fetched_at, checked_at, status)`, where status is `live` or `gone`. `hash` means "what Qdrant currently holds" (sha256 of the extracted text). It is written only after a successful upsert. The crawl inserts or updates rows with hash NULL. No runs table, no sitemaps table: the cron log is the run record, and sitemaps are derived (seeds plus `subsite_sitemap()` over the distinct wp.stolaf.edu first path segments in the db). The DB path is a module constant (`DB`) and connections open lazily per call, never at import, so tests can monkeypatch it like `MANIFEST` today.
2. **One write path, three callers.** `index_page(row)` extracts and chunks, then: write raw file (already done by the fetch), upsert chunks, delete that URL's points whose chunk index >= the new chunk count, then commit `hash`. A crash in between leaves hash != content, and the next run repairs it. Never write the hash if the embedding call failed. `ole.index` indexes rows whose hash is NULL or stale. `ole.index --full` recreates the collection and indexes everything. `ole.refresh` = fetch pass + the same `index_page()`. A Qdrant keyword payload index on `url`. The cross-page text dedupe is dropped entirely; re-run the eval once to confirm retrieval still passes.
3. **Sitemap pass.** Seeds plus derived sub-site sitemaps. Skip child sitemaps whose lastmod in the index is unchanged (verify Jetpack honors this on the first run). A page URL that is new, or whose lastmod differs from stored (compare "differs", not "newer": wp uses timestamps, catalog uses dates, and nulls exist), gets fetched. Unchanged: set `checked_at` only, no request.
4. **Rotation.** `WHERE status='live' ORDER BY checked_at LIMIT 300` (configurable): plain GET, hash compare. This covers link-discovered pages, PDFs, widget changes that lastmod misses, and public Google Doc exports (kind text, source gdoc_export). drive_mcp rows are skipped and counted.
5. **Removal.** One strike: a 404/410 marks the row `gone` and deletes its points by url filter (WordPress 404s are deterministic; blips arrive as 503, which never counts as a removal). The row is kept so links don't re-fetch it nightly; rotation or a sitemap lastmod change heals a false positive. A redirect to another URL marks the source `gone` and enqueues the target. A 404 found through a link also gets a `gone` row.
6. **Lock.** `fcntl.flock` (non-blocking) on `data/crawl.lock`, shared by crawl, index and refresh. If it's taken: print a message, exit 1.
7. **Blocked nights.** In refresh, a Retry-After >= 300 s aborts the run with a log line and exit 1 instead of sleeping (the interactive crawl keeps sleeping). Each run prints the changed, added and gone URLs, so the first nightly run shows hash churn from dynamic page parts (if hundreds change with unchanged lastmod, hash main content only).
8. **URL normalization.** `normalize()` adds a trailing slash on wp.stolaf.edu paths whose last segment has no dot (`/classics` and `/classics/` are the same page). The migration merges such twins.
9. **Migration.** A one-time import of `manifest.jsonl` into `pages.db` (hash NULL, so the next `ole.index` re-embeds everything; about 4k chunks, cents), with the old file kept as `manifest.jsonl.bak`. `ole.crawl`, `ole.add_doc`, `ole.index` and the eval read and write the db. `google_pending.jsonl` stays.
10. **Visibility.** `GET /api/status` = `SELECT max(checked_at), count(*) FROM pages WHERE status='live'`. The chat footer shows "Knowledge updated <date>".
11. **Scheduling, documented only:** a README cron line with the absolute `uv` path (`.env` is loaded by `ole.config`).

## Expected cost per nightly run

About 50 to 300 sitemap requests (unchanged sub-site sitemaps are skipped) plus 300 rotating re-checks plus the changed pages: roughly 40 to 60 minutes at 4 s per request. Embeddings for a few dozen changed pages cost under $0.01. No Claude calls.

## Testing

Each piece is test-first with `httpx.MockTransport` and a temp db/Qdrant collection where needed: migration round-trip; sitemap lastmod diff (new / changed / unchanged); conditional GET 304 vs 200; 404-twice removal; unchanged-hash skip; incremental index update leaving no orphan chunks after a page shrinks; lock prevents concurrent runs; `/api/status`. An integration test runs refresh against a real test collection.

## Open questions for the reviewer

- Is SQLite plus a lock file the right weight here, or overkill compared with keeping JSONL?
- Is "two consecutive 404s means removed" right, or should it be one, or time-based?
- Is the rotating cap of 300 re-checks per night sensible against ~7,000 pages (a full rotation about every 3 weeks)?
- Anything in the existing code that makes this harder than the design assumes?
