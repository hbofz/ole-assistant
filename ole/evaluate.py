"""Score Lefse against eval/questions.jsonl.

Run: uv run python -m ole.evaluate [--model claude-haiku-5-5] [--only ID ...] [--questions eval/heldout.jsonl]
Answers are cached in data/eval_cache, so re-running after a grading change doesn't re-pay for answers.
--rep N gets fresh answers (a separate cache slot), to measure answer noise; --retrieval-only skips answers and grading.
eval/heldout.jsonl is held out: score it to confirm a gain, never tune on its failures.
"""
import argparse
import hashlib
import json
import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ole import answer
from ole.answer import answer_text, retrieve
from ole.search import FALLBACKS
from ole.claude_cli import ask
from ole.config import ANSWER_MODEL, DATA_DIR, GRADER_MODEL, PROMPT_VERSION, ROOT

QUESTIONS = ROOT / "eval" / "questions.jsonl"
CACHE = DATA_DIR / "eval_cache"
RUNS = DATA_DIR / "eval_runs"
BARS = {"retrieval_hit": 0.90, "correct": 0.85}
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}  # $ per 1M tokens in / out

GRADER_PROMPT = """You grade answers from a St. Olaf College chatbot. Reply with JSON only.

Fields:
- "reason": one or two sentences comparing the bot's answer to the expected answer, written before deciding.
- "declined": true if the bot said it could not find the answer, or politely declined, instead of answering. Pointing the student to an office counts as declining. False if it gave a substantive answer.
- "correct": for an answerable question, true if the bot's answer agrees with the expected answer on the facts that matter and adds no wrong facts (extra correct detail is fine, a missing minor detail is fine). Stating the expected fact with a brief caveat (such as "an older page says X") is correct. Stating it and then saying it can't be confirmed, or offering a conflicting value as equally likely, is not correct. For an unanswerable question, true only if the bot declined and did not invent specifics."""

GRADE_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "correct": {"type": "boolean"}, "declined": {"type": "boolean"}},
    "required": ["reason", "correct", "declined"],
    "additionalProperties": False,
}


def cache_key(model: str, question: str, history: list, hit_ids: list[str], rep: int = 0) -> str:
    blob = json.dumps([model, question, history, hit_ids, PROMPT_VERSION] + ([rep] if rep else []))
    return hashlib.sha256(blob.encode()).hexdigest()


def _same_page(a: str, b: str) -> bool:
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def grade(row: dict, reply: str, votes: int = 3) -> dict:
    """Majority of `votes` independent grades (one grade flips on about 1 item in 20)."""
    gs = [_grade_once(row, reply) for _ in range(votes)]
    yes = sum(g["correct"] for g in gs)
    correct = yes * 2 > votes
    pick = next(g for g in gs if g["correct"] == correct)
    return {**pick, "declined": sum(g["declined"] for g in gs) * 2 > votes, "votes": yes}


def _grade_once(row: dict, reply: str) -> dict:
    expected = row["expected_answer"] if row["answerable"] else \
        "UNANSWERABLE. St. Olaf's public pages don't cover this (or it isn't about St. Olaf). The right behavior is to say so and decline."
    # graded on the subscription (headless Claude Code); answers stay on the API, the path being measured
    return ask(f"Question: {row['question']}\n\nExpected: {expected}\n\nBot answer:\n{reply}", GRADER_PROMPT,
               GRADE_SCHEMA, model=GRADER_MODEL)


def summarize(results: list[dict]) -> dict:
    ans = [r for r in results if r["answerable"]]
    unans = [r for r in results if not r["answerable"]]
    hit = sum(bool(r["retrieval_hit"]) for r in ans) / len(ans) if ans else None
    correct = sum(bool(r["correct"]) for r in ans) / len(ans) if ans else None
    confident_wrong = sum(not r["declined"] for r in unans)
    passed = (hit is None or hit >= BARS["retrieval_hit"]) and \
        (correct is None or correct >= BARS["correct"]) and confident_wrong == 0
    return {"retrieval_hit": hit, "correct": correct, "confident_wrong": confident_wrong, "passed": passed,
            "n_answerable": len(ans), "n_unanswerable": len(unans)}


def cost() -> float:
    return sum(PRICES.get(m, (2.0, 10.0))[0] * i / 1e6 + PRICES.get(m, (2.0, 10.0))[1] * o / 1e6
               for m, i, o in answer.USAGE)


def _rank(urls: list[str], expected: list[str]) -> int | None:
    return next((i for i, u in enumerate(urls, 1) if any(_same_page(u, e) for e in expected)), None)


def score(row: dict, model: str, rep: int = 0, retrieval_only: bool = False, votes: int = 3) -> dict:
    history = row.get("history", [])
    hits = retrieve(history, row["question"])
    urls = [h.url for h in hits]
    res = {**row, "retrieved": urls, "rank": _rank(urls, row["expected_urls"]) if row["answerable"] else None}
    res["retrieval_hit"] = res["rank"] is not None if row["answerable"] else None
    if retrieval_only:
        return res
    # hit ids plus a hash of their text: the same hit can come with or without a glued-on neighbor chunk
    ids = [f"{h.id}:{hashlib.sha256(h.text.encode()).hexdigest()[:12]}" for h in hits]
    key = CACHE / f"{cache_key(model, row['question'], history, ids, rep)}.json"
    if key.exists():
        cached = json.loads(key.read_text())
    else:
        text, sources, _ = answer_text(history, row["question"], model, hits)
        cached = {"text": text, "sources": sources}
        if "[ERROR:" not in text:
            key.write_text(json.dumps(cached))
    g = grade(row, cached["text"], votes)
    res |= {"answer": cached["text"], **g}
    if not row["answerable"]:
        res["correct"] = g["declined"] and g["correct"]
    return res


def run(model: str, only: set[str] | None = None, questions: Path = QUESTIONS, rep: int = 0,
        retrieval_only: bool = False, workers: int = 6, votes: int = 3) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(l) for l in Path(questions).read_text().splitlines() if l.strip()]
    if only:
        rows = [r for r in rows if r["id"] in only]
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(lambda r: score(r, model, rep, retrieval_only, votes), rows))
    for res in results:
        if retrieval_only:
            print(f"[{'ok ' if res['retrieval_hit'] is not False else 'BAD'}] {res['id']:>4} rank={res['rank']}")
        else:
            print(f"[{'ok ' if res['correct'] else 'BAD'}] {res['id']:>4} hit={res['retrieval_hit']} "
                  f"declined={res['declined']} | {res['reason'][:90]}", flush=True)
    ans = [r for r in results if r["answerable"]]
    summary = summarize(results) if not retrieval_only else {"retrieval_hit": sum(r["retrieval_hit"] for r in ans) / len(ans)}
    return {"model": model, "questions": str(questions), "rep": rep, "summary": summary,
            "cost_usd": round(cost(), 4), "rerank_fallbacks": FALLBACKS[0], "results": results}


def compare(qset: str, a: str, b: str) -> dict:
    """Paired comparison of two tagged configs on one question set: each item's correct rate averaged over
    its reps, then a bootstrap 95% CI of the mean difference (b - a) over items."""
    def load(tag):
        per = {}
        for f in RUNS.glob(f"*-{qset}-{tag}-rep*.json"):
            d = json.loads(f.read_text())
            if "correct" in d["summary"]:
                for r in d["results"]:
                    per.setdefault(r["id"], []).append(bool(r["correct"]))
        return {k: sum(v) / len(v) for k, v in per.items()}
    A, B = load(a), load(b)
    ids = sorted(set(A) & set(B))
    diff = [B[i] - A[i] for i in ids]
    rng = random.Random(0)
    boots = sorted(sum(rng.choice(diff) for _ in diff) / len(diff) for _ in range(10000))
    return {"a": sum(A[i] for i in ids) / len(ids), "b": sum(B[i] for i in ids) / len(ids), "diff": sum(diff) / len(diff),
            "ci": (boots[250], boots[9750]), "better": [i for i in ids if B[i] > A[i]], "worse": [i for i in ids if B[i] < A[i]]}


def report(out: dict) -> None:
    if out["rerank_fallbacks"]:
        print(f"WARNING: Voyage failed {out['rerank_fallbacks']} times; those questions used the local reranker")
    if "correct" not in out["summary"]:  # --retrieval-only
        print(f"\nretrieval {out['summary']['retrieval_hit']:.1%}")
        return
    by_area = defaultdict(list)
    for r in out["results"]:
        by_area[r.get("area", "?")].append(r)
    print(f"\n{'area':<22}{'n':>3}{'correct':>9}{'hit':>6}")
    for area, rs in sorted(by_area.items()):
        ans = [r for r in rs if r["answerable"]]
        c = sum(bool(r["correct"]) for r in rs)
        h = sum(bool(r["retrieval_hit"]) for r in ans)
        print(f"{area:<22}{len(rs):>3}{c:>6}/{len(rs):<2}{h:>3}/{len(ans)}")
    s = out["summary"]
    fmt = lambda v: "n/a" if v is None else f"{v:.0%}"
    print(f"\nmodel {out['model']}: retrieval {fmt(s['retrieval_hit'])}, correct {fmt(s['correct'])}, "
          f"confident wrong {s['confident_wrong']} -> {'PASS' if s['passed'] else 'FAIL'}  (spent ${out['cost_usd']:.2f})")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Score Lefse against the eval set.")
    p.add_argument("--model", default=ANSWER_MODEL)
    p.add_argument("--only", nargs="*")
    p.add_argument("--questions", type=Path, default=QUESTIONS)
    p.add_argument("--rep", type=int, default=0)
    p.add_argument("--retrieval-only", action="store_true")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--votes", type=int, default=3, help="grades per answer, majority wins")
    p.add_argument("--tag", default="", help="label for the run file")
    p.add_argument("--compare", nargs=2, metavar=("TAG_A", "TAG_B"), help="compare two tagged configs' saved runs")
    a = p.parse_args()
    if a.compare:
        c = compare(a.questions.stem, *a.compare)
        print(f"{a.questions.stem}: {a.compare[0]} {c['a']:.1%} -> {a.compare[1]} {c['b']:.1%}  diff {c['diff']:+.1%}  "
              f"95% CI [{c['ci'][0]:+.1%}, {c['ci'][1]:+.1%}]\n  better: {c['better']}\n  worse: {c['worse']}")
        raise SystemExit
    out = run(a.model, set(a.only) if a.only else None, a.questions, a.rep, a.retrieval_only, a.workers, a.votes)
    RUNS.mkdir(parents=True, exist_ok=True)
    name = "-".join(x for x in (time.strftime('%Y%m%d-%H%M%S'), a.model, a.questions.stem, a.tag, f"rep{a.rep}") if x)
    (RUNS / f"{name}.json").write_text(json.dumps(out, indent=1))
    report(out)
