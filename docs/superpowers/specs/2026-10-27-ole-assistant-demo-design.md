# ole-assistant demo: design

Date: 2026-10-27. Owner: Hamzah (AI research team, Piper Center, St. Olaf College).

## Intent

St. Olaf students have to dig through dozens of college web pages to find answers about courses, credits, majors, internships, the Piper Center, other offices and centers, and so on. ole-assistant is a chatbot that answers any question about St. Olaf from the college's public information, and cites its sources as links so students can check them and go deeper.

This first build is a **proof-of-concept demo**. It runs on Hamzah's machine and exists to show the Piper Center that the idea works. Deploying a real version for students on a proper server is a later, separate project.

## Goals

- Index St. Olaf's public web content: wp.stolaf.edu (the main site and the office/department sub-sites) plus catalog.stolaf.edu (the course catalog).
- Also index Google Docs that those pages link to.
- Answer free-form student questions accurately, using only the indexed content, with numbered citations linking to the source pages.
- When the index has no answer, say so plainly and point the student to the most relevant office or page. Never make an answer up.
- A simple local web page with a chat box, good enough to demo live to Piper Center staff.
- Show measurably that it works: an evaluation set of about 50 questions with known answers, plus a script that scores the bot against them.

## Non-goals (for this demo)

- Deployment to a server, accounts or logins, multiple simultaneous users, scaling.
- Non-public or logged-in content (MyOle, Moodle, the student information system).
- Automatic scheduled re-crawls. A command that re-runs the crawl and index by hand is enough.
- Remembering conversations across page reloads. Follow-up questions within one chat session should work.

## Anti-goals (would make it a failure even if it runs)

- Confident answers that aren't supported by a cited source, or citations that point to a page that doesn't say what was claimed.
- Hammering St. Olaf's servers, or crawling paths that robots.txt disallows.
- Indexing anything that isn't meant to be public.

## Decided

- **Approach: RAG, no fine-tuning.** Retrieve relevant chunks from our own index, and Claude answers from them.
- **Vector database: Qdrant** (open source), run locally as a Docker container with a persistent volume. Chosen because it supports hybrid search (keyword plus semantic), which matters for exact terms like course codes ("CSCI 251") and office names, and because the same setup carries over to a real server later.
- **Embeddings: OpenAI `text-embedding-3-large`** (1024 dims), with Hamzah's OpenAI key in `.env`. Changed during the build: the local open-source model measured ~1 s per chunk on this CPU. Keyword search (BM25) and the reranker (`ms-marco-MiniLM-L-6-v2`) still run locally.
- **The Claude API writes the answers** (OpenAI is used only for embeddings). **No Opus (Hamzah's call).** Answers use Claude Haiku 5.5 (`claude-haiku-5-5`), Hamzah's call after it matched Sonnet 5.5 on the eval at ~1/20 the cost. Sonnet 5.5 grades the eval. The answer model is a single config value. Answers use Claude's built-in citation support so citations map to real retrieved chunks.
- **Language: Python.** Project lives in `~/projects/ole-assistant`.
- **Crawling uses a plain crawler, not LLM agents.** It starts from the sitemaps: wp.stolaf.edu (root `sitemap.xml` is a Jetpack sitemap index of about 3,000 URLs; sub-sites such as `/registrar/` have their own sitemaps; the root sitemap is ~99% news posts. The Piper Center site is `wp.stolaf.edu/pipercenter/`) and catalog.stolaf.edu (`sitemap.xml`, about 125 URLs). It obeys robots.txt (note: wp.stolaf.edu disallows `/catalog/` and `/files/`, while catalog.stolaf.edu is crawlable) and fetches at about 1 request per second with an identifying User-Agent.
- **Google Docs:** the crawler collects every docs.google.com link it finds on the crawled pages. Only Docs linked from the public site are in scope. Hamzah's own Drive is not crawled. Docs shared publicly ("anyone with the link") are exported as text by the crawler (`.../export?format=txt`). Docs that need a login are fetched by Claude Code through Hamzah's Google Drive MCP during the build, and are cited by their Google Docs URL.
- **The Anthropic API key** is in `.env` at the project root (mode 600, gitignored, verified working). It is loaded into the environment and never committed or printed. Crawling and indexing work without it.

## Calls made by Claude (Hamzah can override)

- Raw crawled pages are cached on disk so re-indexing (changing chunking or embeddings) doesn't require re-crawling.
- Skip time-bound noise: news posts, calendar/event pages, and tag/archive listing pages. Keep everything else.
- Each chunk keeps its source URL, page title, section heading path and the page's last-modified date (from the sitemap). The heading path is included in the chunk text for retrieval.
- Answers stream to the page. Each answer lists its sources as numbered, clickable links.
- The system prompt tells Claude it is a St. Olaf information assistant: answer only from the provided sources, say when a source may be out of date, and refer to the right office when unsure.
- On Sonnet 5.5, enable the API's server-side refusal fallback (`fallbacks: "default"`). Haiku 5.5 has no server-side fallback, so a refusal there shows a polite "can't help with that" message.
- **Which login-required Google Docs get indexed:** check each one's sharing settings (Drive MCP `get_file_permissions`). Index it if it is shared with "anyone at St. Olaf" (stolaf.edu domain), since every student can open that citation. Skip it if it is shared only with specific people. Index entries record which path a Doc came through (public export or MCP). Re-crawling doesn't refresh the MCP-fetched Docs; that is a manual step for the demo.
- **What counts as working:** retrieval finds the expected source for at least 90% of questions, at least 85% of answers are graded correct, and the bot gives **zero** confident wrong answers on the questions the site can't answer. Of the two ways to fail, a confident wrong answer is worse than an unnecessary "I don't know", so tuning favors caution. Below the bar, tune (chunking, hybrid weighting, top-k, reranking) and re-run.
- **Quality bar: the best demo possible, with no deadline.** It should be fully working, not a mock-up. PDFs hosted on the site are indexed, and reranking is used if it helps the eval scores.
- **Budget:** $200 of Anthropic credits. Sonnet 5.5 for answers and grading, Haiku 5.5 as the comparison. Don't waste credits: aim for development and eval spend under about $100, leaving the rest for demos.
- Eval runs cache answers by (question, retrieved chunk IDs, prompt version), so re-scoring after a grading change doesn't re-pay for answers. A full run is about 100 Sonnet 5.5 calls, roughly $2 to $4.
- Claude drafts the ~50 evaluation questions from the crawled site, each with its expected answer and source URL, covering the main areas (courses/catalog, requirements and credits, registrar, Piper Center, internships, financial aid, residence life, other centers). Hamzah reviews them. The score covers retrieval (was the right source retrieved?) and answer correctness (graded by Claude against the expected answer). A handful of the questions should have no answer on the site, to test that the bot admits it doesn't know.

## Left to the builder

- Library choices: HTML-to-text extraction, chunking approach and size, which open-source dense and sparse embedding models, the web framework, the frontend.
- The exact URL filters for "time-bound noise", based on what the sitemaps actually contain.
- How many chunks to retrieve, and which reranker to use.
- Project layout and commands, kept minimal.
