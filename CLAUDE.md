# Lefse (ole-assistant): notes for coding agents

Lefse is a chatbot that answers St. Olaf College students' questions from St. Olaf's **public** websites, with numbered citations to the page each fact came from. It started as a proof-of-concept for the Piper Center AI research team and is now aimed at all students. The owner is Hamzah. README.md has the full run, test and deploy instructions; this file covers what an agent needs on top of that.

## How a question flows (ole/answer.py, ole/search.py)

1. **Rewrite** (Claude Haiku 5.5, `rewrite_prompt()`): the question plus the last 6 turns becomes a short keyword query, using a small St. Olaf glossary ("pass/fail" becomes "S/U grading", "J-Term" becomes "Interim"). It is told today's date and academic year, and adds the year to date, deadline, hours and event questions.
2. **Hybrid search** in Qdrant (collection `stolaf`) for both the rewrite and the student's own words (the rewrite sometimes drops the words the right page uses): OpenAI `text-embedding-3-large` (1024 dims) dense top 40 plus BM25 sparse top 40 for each, merged with RRF into 80.
3. **Rerank** the top `RERANK_K`=60 with Voyage `rerank-3`, query = the student's words then the rewrite. 429s are retried briefly; after that the local MiniLM is the fallback, and the eval reports every fallback (a silent fallback once skewed results).
4. **Diversify** to at most 3 chunks per URL, keep `ANSWER_K`=8, and glue each hit's previous and next chunk onto it.
5. **Answer** (Claude Haiku 5.5, effort low, SYSTEM_PROMPT): chunks go in as `search_result` blocks with citations on, with titles carrying "(updated YYYY-MM-DD)" or "(date unknown)". Conflict notes from `ole.conflicts` are added when a hit sits on the outdated side of a known contradiction.
6. **Citations** are numbered per source URL and moved to sentence ends (`place_cites`). The UI (ole/static) renders markdown with render.js.

A question takes about 5 to 8 s and costs about $0.002 (half of it Voyage reranking 60 chunks; the first 200M Voyage tokens are free).

## Data and jobs

- `data/pages.db` (SQLite, `ole/db.py`): one row per page (status live/gone, hash = what Qdrant holds), plus the facts and conflicts tables. About 9,811 live pages and 38k chunks.
- Extraction (`ole/extract.py`, `ole/chunk.py`): trafilatura markdown split by headings. WordPress accordion titles (`button.accordion__header`) become headings, headings with nothing under them (h4/h5 used as styled paragraphs) are kept as text, and short sections folded together keep each heading above its own text. Changing extraction changes every page hash, so re-index with `--full` after.
- `ole.crawl` is the full crawl, polite: robots.txt, a 4 s delay, Retry-After backoff, login-walled sub-sites skipped, redirect sources recorded. St. Olaf's firewall bans anything faster for an hour. Never lower CRAWL_DELAY.
- `ole.refresh` runs nightly at 03:07 (systemd user timer): a sitemap diff plus a rotation of 300 pages, then incremental indexing.
- `ole.conflicts extract|find|report` runs nightly at 05:37: Haiku extracts checkable facts, Sonnet judges contradictions, and the report goes to `/conflicts`. This runs through **headless Claude Code on Hamzah's Max subscription** (`ole/claude_cli.py`, with ANTHROPIC_API_KEY stripped), not the API.
- Services: `systemctl --user status lefse.service` serves the app on 127.0.0.1:8000. It's public via `tailscale funnel --https=8443` at https://aspire.taild7f840.ts.net:8443, login-gated by `LEFSE_PASSWORD`.
- Keys live in `.env` (ANTHROPIC_API_KEY, OPENAI_API_KEY, VOYAGE_API_KEY, LEFSE_PASSWORD). Never print them or commit them.

## Eval (the accuracy work starts here)

- `uv run python -m ole.evaluate` scores `eval/questions.jsonl` (78 items: 68 answerable incl. 5 follow-ups with history, 8 unanswerable, 2 off-topic). Answers use the real API path; grading uses Sonnet via claude -p (subscription), 3 votes, majority wins. Cached answers are keyed by (model, question, history, hit ids, PROMPT_VERSION), so bump PROMPT_VERSION when the prompt changes. Flags: `--questions`, `--rep N` (fresh answers), `--retrieval-only`, `--tag`, `--compare TAG_A TAG_B` (paired per-item comparison with a bootstrap CI over saved runs).
- `eval/questions.jsonl` was used for tuning: it's the train set. **`eval/heldout.jsonl`** (60 items, h01-h60: 50 answerable incl. 5 follow-ups, 8 decline cases incl. 2 personal-record questions, 2 off-topic; no expected URL overlaps the train set) is held out. Confirm every gain there and never tune on its failures (look at aggregate scores and item ids only).
- Pass bar: retrieval >= 90%, correct >= 85%, confident wrong (answering an unanswerable) = 0.
- Noise: the grader is consistent (138 answers x 4 grades: 0 disagreements; 3-vote splits on 1.7% of answers). Run-to-run noise of 2 to 4 points comes from answer sampling and the query rewrite, so compare over 2+ train reps and 3 held-out reps.
- Latest (2026-10-29, fixed-extraction index, dual query, rerank 60, prompt v7, date-aware rewrite): **train 96.2%, held-out 95.0%** correct (held-out +7.8 over the previous setup, 95% CI +1.1 to +15.0), retrieval 96-100%, confident wrong 0. Persistent train misses: q40 (regular dining hours outranked by event/conference/summer pages), q29 (answer sits in a footnote).
- Answer-model head-to-head (2026-10-29, same hits, prompt and grader; OpenAI got numbered text sources): Haiku 95.5% train / 98.3% held-out. gpt-6-luna (same price) -5 on both and answered 2 unanswerables per held-out rep; gpt-6.1-terra (10x price) -2.6 / -1.1; gpt-6.1-sol (20x) +0 / -2.8. OpenAI answers were faster (2 to 5 s vs ~5 s) but none beat Haiku, so Haiku stays.
- Tried and rejected on 2026-10-29: answer effort medium (wash, +2 s), 12 hits instead of 8 (wash, +25% cost), a Voyage rerank instruction to prefer current pages (neutral), contextual chunk headers (Anthropic's contextual retrieval: +train, -held-out MRR).
- `eval/questions.v1.jsonl` is the old 48-item set from the partial crawl (stale).
- Runs and caches live in data/eval_runs/ and data/eval_cache/. `data/corpus/*.md` is a text dump of every live page (URL/TITLE/UPDATED header), good for grepping ground truth; regenerate it after a recrawl.

## Rules from the owner (follow them)

- Don't commit or push until a change is verified working and Hamzah has confirmed. Check pytest's **own exit code** (a pipe into tail once hid failures and a broken commit was pushed).
- Test-first for code changes. Fast suite: `uv run pytest -q`; integration (needs Qdrant): `-m integration`; live API: `-m live`.
- Models: Claude Haiku 5.5 answers, Sonnet 5.5 judges and grades. **No Opus.** Offline analysis and eval grading go on the subscription (claude -p); live answers for users stay on the API.
- Minimal code, stdlib first, no speculative abstractions. Never use em dashes in anything written (code comments, UI copy, docs).
- Site copy is for all St. Olaf students, calm, and never says "AI". Lefse only uses public pages and must say it can't see personal records (schedule, grades, degree audit, aid package).
- Installing anything persistent outside the repo (systemd units, cron) needs Hamzah's explicit OK. Unit files live in deploy/.
- `docs/superpowers/` holds local design docs and plans and is gitignored on purpose. Don't untrack/rebase in ways that delete them (that happened once).
