"""Evaluate `cnlp ask` against tests/fixtures/ask/questions.yaml (live model).

Usage: uv run python -I scripts/eval_ask.py [OUT.json] [--config audit.yaml] [--only ID,ID]
Prints per-kind accuracy, command-match accuracy and the grounding rate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

from cnlp import ask as A
from cnlp import knowledge

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "ask" / "questions.yaml"


def score(e: dict, r: A.AskResult) -> tuple[bool, str]:
    if r.kind != e["kind"]:
        return False, f"kind {r.kind}, want {e['kind']}"
    if e.get("refused") and not r.refused:
        return False, "not refused"
    if e["kind"] == "command":
        argv = (r.command or "").split()[1:]
        want = e["subcommand"].split()
        if argv[: len(want)] != want:
            return False, f"command {r.command!r}, want {e['subcommand']}"
    if e["kind"] == "answer":
        text = (r.answer or "").lower()
        miss = [m for m in e.get("must_mention", []) if m.lower() not in text]
        if miss:
            return False, f"missing {miss}"
        if e.get("no_pass_claim") and re.search(r"\bpassed\b", text) and not re.search(r"\bnot\b|\bno\b|n't", text):
            return False, "claims a pass"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="eval_ask.json")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    entries = yaml.safe_load(CORPUS.read_text())
    if a.only:
        keep = set(a.only.split(","))
        entries = [e for e in entries if e["id"] in keep]
    index = knowledge.build_index(a.config)
    by_kind = defaultdict(lambda: [0, 0])
    grounded = answers = 0
    rows, t0 = [], time.time()
    for e in entries:
        t = time.time()
        r = A.ask(e["q"], a.config, index=index)
        ok, why = score(e, r)
        by_kind[e["kind"]][0] += ok
        by_kind[e["kind"]][1] += 1
        if r.kind == "answer":
            answers += 1
            grounded += r.grounded
        rows.append({"id": e["id"], "pass": ok, "why": why, "seconds": round(time.time() - t, 1),
                     "result": r.to_dict()})
        print(f"{e['id']:<18} {'PASS' if ok else 'FAIL'} {why}", file=sys.stderr)
    Path(a.out).write_text(json.dumps(rows, indent=2) + "\n")
    print(f"\n{'kind':<10}{'pass':>8}")
    for k, (p, n) in sorted(by_kind.items()):
        print(f"{k:<10}{p:>4}/{n:<3}")
    total = sum(v[0] for v in by_kind.values())
    print(f"{'total':<10}{total:>4}/{len(entries)}   in {time.time() - t0:.0f} s")
    if answers:
        print(f"grounding rate: {grounded}/{answers} answers fully grounded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
