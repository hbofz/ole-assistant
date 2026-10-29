"""Score Lefse against eval/questions.jsonl.

Run: uv run python -m ole.evaluate [--model claude-haiku-5-5] [--only ID ...]
Answers are cached in data/eval_cache, so re-running after a grading change doesn't re-pay for answers.
"""
import argparse
import hashlib
import json
import time
from collections import defaultdict

from ole import answer
from ole.answer import answer_text, retrieve
from ole.claude_cli import ask
from ole.config import ANSWER_MODEL, DATA_DIR, GRADER_MODEL, PROMPT_VERSION, ROOT

QUESTIONS = ROOT / "eval" / "questions.jsonl"
CACHE = DATA_DIR / "eval_cache"
RUNS = DATA_DIR / "eval_runs"
BARS = {"retrieval_hit": 0.90, "correct": 0.85}
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}  # $ per 1M tokens in / out

GRADER_PROMPT = """You grade answers from a St. Olaf College chatbot. Reply with JSON only.

Fields:
- "declined": true if the bot said it could not find the answer, or politely declined, instead of answering. Pointing the student to an office counts as declining. False if it gave a substantive answer.
- "correct": for an answerable question, true if the bot's answer agrees with the expected answer on the facts that matter and adds no wrong facts (extra correct detail is fine, a missing minor detail is fine). For an unanswerable question, true only if the bot declined and did not invent specifics.
- "reason": one short sentence."""

GRADE_SCHEMA = {
    "type": "object",
    "properties": {"correct": {"type": "boolean"}, "declined": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["correct", "declined", "reason"],
    "additionalProperties": False,
}


def cache_key(model: str, question: str, history: list, hit_ids: list[str]) -> str:
    blob = json.dumps([model, question, history, hit_ids, PROMPT_VERSION])
    return hashlib.sha256(blob.encode()).hexdigest()


def _same_page(a: str, b: str) -> bool:
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def grade(row: dict, reply: str) -> dict:
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


def run(model: str, only: set[str] | None = None) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(l) for l in QUESTIONS.read_text().splitlines() if l.strip()]
    if only:
        rows = [r for r in rows if r["id"] in only]
    results = []
    for row in rows:
        history = row.get("history", [])
        hits = retrieve(history, row["question"])
        key = CACHE / f"{cache_key(model, row['question'], history, [h.id for h in hits])}.json"
        if key.exists():
            cached = json.loads(key.read_text())
        else:
            text, sources, _ = answer_text(history, row["question"], model, hits)
            cached = {"text": text, "sources": sources}
            if "[ERROR:" not in text:
                key.write_text(json.dumps(cached))
        g = grade(row, cached["text"])
        urls = [h.url for h in hits]
        res = {**row, "retrieved": urls, "answer": cached["text"], **g,
               "retrieval_hit": any(_same_page(u, e) for u in urls for e in row["expected_urls"]) if row["answerable"] else None}
        if not row["answerable"]:
            res["correct"] = g["declined"] and g["correct"]
        results.append(res)
        flag = "ok " if res["correct"] else "BAD"
        print(f"[{flag}] {row['id']:>4} hit={res['retrieval_hit']} declined={g['declined']} | {g['reason'][:90]}", flush=True)
    return {"model": model, "summary": summarize(results), "cost_usd": round(cost(), 4), "results": results}


def report(out: dict) -> None:
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
    a = p.parse_args()
    out = run(a.model, set(a.only) if a.only else None)
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.model}.json").write_text(json.dumps(out, indent=1))
    report(out)
