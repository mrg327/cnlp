"""Evaluate the NL -> controlled-English translator against tests/fixtures/nl/requests.yaml.

Usage: uv run python -I scripts/eval_translate.py [OUT.json] [-n SAMPLES]
Needs the local model server (see src/cnlp/llm.py). Prints a summary table.

Scoring, per category:
  clear           translation's pins (gpio, mode, pull, function) and features equal the expected spec
  state_machine   parses, no FSM errors, same number of machines and states as expected
  ambiguous       at least one question, or the samples disagreed
  rule_violation  every expected rule ID is raised on the translation, or a question was asked
  out_of_scope    at least one question
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

from cnlp import cnl, fsm, nl, rules

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "nl" / "requests.yaml"


def pin_key(m):
    return sorted((p.gpio, p.mode, p.pull, p.function) for p in m.pins), m.features


def score(entry: dict, r) -> tuple[bool, str]:
    cat = entry["category"]
    if cat in ("ambiguous", "out_of_scope"):
        ok = bool(r.questions) or not r.agreed
        return ok, "" if ok else "no question asked"
    if r.manifest is None:
        return False, "no sample parsed"
    if cat == "clear":
        want = cnl.parse(entry["expected_spec"])
        ok = pin_key(r.manifest) == pin_key(want)
        return ok, "" if ok else f"got {pin_key(r.manifest)[0]} want {pin_key(want)[0]}"
    if cat == "state_machine":
        want = cnl.parse(entry["expected_spec"])
        errs = [f.rule for f in fsm.check(r.manifest) if f.severity == "error"]
        shape = lambda m: sorted(len(sm.states) for sm in m.state_machines)  # noqa: E731
        ok = not errs and shape(r.manifest) == shape(want)
        return ok, "" if ok else f"fsm errors {errs}, states {shape(r.manifest)} want {shape(want)}"
    if cat == "rule_violation":
        got = {f.rule for f in rules.check(r.manifest) + fsm.check(r.manifest)}
        missing = set(entry.get("expected_rules", [])) - got
        ok = not missing or bool(r.questions)
        return ok, "" if ok else f"missing rules {sorted(missing)}"
    return False, f"unknown category {cat}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="eval_translate.json")
    ap.add_argument("-n", type=int, default=3)
    a = ap.parse_args()
    entries = yaml.safe_load(CORPUS.read_text())
    rows, by_cat = [], defaultdict(lambda: [0, 0, 0, 0])  # pass, total, parsed, agreed
    t0 = time.time()
    for e in entries:
        t = time.time()
        r = nl.translate(e["request"], n=a.n)
        ok, why = score(e, r)
        c = by_cat[e["category"]]
        c[0] += ok
        c[1] += 1
        c[2] += r.ok
        c[3] += r.agreed
        rows.append({"id": e["id"], "category": e["category"], "pass": ok, "why": why,
                     "agreed": r.agreed, "parsed": r.ok, "parse_failures": r.parse_failures,
                     "questions": r.questions, "spec": r.spec_text, "seconds": round(time.time() - t, 1)})
        print(f"{e['id']:<14} {'PASS' if ok else 'FAIL'} {why}", file=sys.stderr)
    Path(a.out).write_text(json.dumps(rows, indent=2) + "\n")
    print(f"\n{'category':<16}{'pass':>8}{'parsed':>8}{'agreed':>8}")
    for cat, (p, n, parsed, agreed) in sorted(by_cat.items()):
        print(f"{cat:<16}{p:>4}/{n:<3}{parsed:>5}/{n:<3}{agreed:>4}/{n}")
    total = sum(v[0] for v in by_cat.values())
    print(f"{'total':<16}{total:>4}/{len(entries)}   in {time.time() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
