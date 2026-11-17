"""Evaluate `cnlp ask` against tests/fixtures/ask/questions.yaml (live model).

Usage: uv run python -I scripts/eval_ask.py [OUT.json] [--config audit.yaml] [--only ID,ID]
Prints per-kind accuracy, command-match accuracy and the grounding rate.
Entry keys: kind, subcommand, cmd_contains, cmd_parses, must_mention, refused, no_pass_claim,
must_mention_any (at least one), sources_include (the answer must cite a source id containing one of these strings).
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
    # `kind` may list alternatives, e.g. [command, answer]; the first is the one tallied.
    kinds = e["kind"] if isinstance(e["kind"], list) else [e["kind"]]
    if r.kind not in kinds:
        return False, f"kind {r.kind}, want {' or '.join(kinds)}"
    e = {**e, "kind": r.kind}
    if e.get("refused") and not r.refused:
        return False, "not refused"
    if e["kind"] == "command":
        argv = (r.command or "").split()[1:]
        want = e["subcommand"].split()
        if argv[: len(want)] != want:
            return False, f"command {r.command!r}, want {e['subcommand']}"
        for frag in e.get("cmd_contains", []):
            if frag.lower() not in (r.command or "").lower():
                return False, f"command {r.command!r} lacks {frag!r}"
        if e.get("cmd_parses") and "--help" in (r.command or ""):  # wanted a concrete pin line
            return False, "fell back to --help, wanted a concrete pin line"
    if e["kind"] == "answer":
        text = (r.answer or "").lower()
        miss = [m for m in e.get("must_mention", []) if m.lower() not in text]
        if miss:
            return False, f"missing {miss}"
        anys = e.get("must_mention_any", [])
        if anys and not any(m.lower() in text for m in anys):
            return False, f"missing any of {anys}"
        if e.get("no_pass_claim") and re.search(r"\bpassed\b", text) and not re.search(r"\bnot\b|\bno\b|n't", text):
            return False, "claims a pass"
    want = e.get("sources_include", [])
    if want and not any(any(w in sid for w in want) for sid in r.sources):
        return False, f"sources {r.sources} lack any of {want}"
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
        k = e["kind"][0] if isinstance(e["kind"], list) else e["kind"]
        by_kind[k][0] += ok
        by_kind[k][1] += 1
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
