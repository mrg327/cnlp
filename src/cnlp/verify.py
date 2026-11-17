"""verify-plan: concrete, deterministic verification steps per requirement. No model calls.

Reads the target's spec only through a temp snapshot (see `evidence._spec_for`). Test names,
methods and pass criteria come from the spec's "Verification plan" table; everything the spec
leaves in prose (probe pin, measurement kind, criterion in `cnlp measure` syntax, key layout)
comes from per-requirement `verify:` hints in requirements.yaml. A missing hint is reported as
a step, never guessed.

plan() returns {"requirements": [row, ...], "notes": [...]}; each row is
{req, text, kind, tests: [{name, method, criterion}], evidence_types, status, steps,
 checklist?}. A step is {"kind": "command"|"note"|"checklist", "text": str, "command"?: str}.
"""

from __future__ import annotations

import itertools
import re
import tempfile
from pathlib import Path
from typing import Any

from cnlp import cnl, evidence, measure

_ROW = re.compile(r"^row_(\d+)$")
_COL = re.compile(r"^col_(\d+)$")
_SCOPE_WORDS = re.compile(r"scope|measure|trace", re.IGNORECASE)
_PREVIEW = 6


def _q(s: str) -> str:
    """Double-quote for a shell, escaping the characters that stay special inside quotes."""
    return '"' + re.sub(r'(["\\$`])', r"\\\1", s) + '"'


def _tests_table(rcfg: dict, target: Path) -> dict[str, dict]:
    """-> {test name: {name, method, criterion, covers: [ids]}} from the Verification plan."""
    rel = str(rcfg.get("spec", "docs/spec.md"))
    with tempfile.TemporaryDirectory(prefix="cnlp-verify-") as tmp:
        snap = evidence._snapshot_file(target, rel, Path(tmp))
        if snap is None:
            raise evidence.EvidenceError(f"spec not found: {target / rel}")
        md = snap.read_text("utf-8")
    out: dict[str, dict] = {}
    for head, rows in evidence._tables(md):
        low = [h.lower() for h in head]
        if "test" not in low or "covers" not in low:
            continue
        ti, ci = low.index("test"), low.index("covers")
        mi = low.index("method") if "method" in low else None
        pi = next((i for i, h in enumerate(low) if "pass" in h), None)
        for r in rows:
            if len(r) <= ci:
                continue
            out[r[ti]] = {"name": r[ti],
                          "method": r[mi] if mi is not None and len(r) > mi else "",
                          "criterion": r[pi] if pi is not None and len(r) > pi else "",
                          "covers": re.findall(r"[A-Z]{1,3}\d+", r[ci])}
    return out


def _pins(acfg: dict, base: Path, notes: list[str]) -> dict[str, int]:
    """-> {role: gpio} from the project's pin plan (spec.cnl)."""
    spec = base / str(acfg.get("spec", "spec.cnl"))
    try:
        m = cnl.parse(spec.read_text("utf-8"))
    except (cnl.CnlError, OSError, ValueError) as e:  # a broken pin plan must not hide the rest of the plan
        notes.append(f"pin plan unreadable ({spec.name}): {e}")
        return {}
    return {p.role: p.gpio for p in m.pins}


def _probe(role: str | None, pins: dict[str, int]) -> str | None:
    if not role:
        return None
    if role in pins:
        return f"GPIO {pins[role]} ({role.replace('_', ' ')})"
    return None


def _validate_criterion(kind: str, criterion: str) -> str | None:
    """None when `cnlp measure --criterion` would accept it for this kind, else the reason."""
    unit = measure.KIND_UNIT[kind]
    probe = {"unit": unit, "mean": 0.0, "max": 0.0, "min": 0.0}
    try:
        measure.check(probe, criterion)
    except (measure.MeasureError, KeyError, ValueError) as e:
        return str(e)
    return None


def _evidence_type(accepts: list[str], hint: dict, measured: bool) -> str:
    if hint.get("evidence"):
        return str(hint["evidence"])
    if measured and "measured_scope" in accepts:
        return "measured_scope"
    for t in ("tested", "reviewed", "measured_serial", "measured_scope"):
        if t in accepts:
            return t
    return accepts[0] if accepts else "tested"


def _manual_add(req: str, etype: str, criterion: str | None, th: dict | None = None) -> str:
    if etype.startswith("measured_"):
        return (f"cnlp evidence add . {req} {etype} pass --value <measured> --unit <unit> "
                f"--criterion {_q(criterion or '<criterion>')}")
    vu = ""
    if th and th.get("value") is not None:
        vu = f" --value {th['value']}" + (f" --unit {th['unit']}" if th.get("unit") else "")
    return f'cnlp evidence add . {req} {etype} pass{vu} --note "<what you did and saw>"'


def _measure_steps(req: str, test: dict, hint: dict, accepts: list[str],
                   pins: dict[str, int]) -> list[dict]:
    name = test["name"]
    kind = str(hint["measure"])
    steps: list[dict] = []
    if kind not in measure.KINDS:
        return [{"kind": "note", "text": f"{name}: hint measure {kind!r} is not a cnlp measure "
                                         f"kind ({', '.join(measure.KINDS)}); fix requirements.yaml"}]
    crit = hint.get("criterion")
    if not crit:
        return [{"kind": "note", "text": f"{name}: criterion missing in requirements.yaml "
                                         f"(verify.tests); spec says: {test['criterion']!r}. Add "
                                         f"one in measure syntax (e.g. '< 1.6 ms') for a ready command."},
                {"kind": "command", "text": "manual fallback",
                 "command": _manual_add(req, "measured_scope", None)}]
    why = _validate_criterion(kind, str(crit))
    if why:
        return [{"kind": "note", "text": f"{name}: hint criterion {crit!r} does not parse: {why}"}]
    ch = int(hint.get("ch", 1))
    p1, p2 = _probe(hint.get("probe"), pins), _probe(hint.get("probe2"), pins)
    wiring = f"scope CH{ch} on {p1 or hint.get('probe') or 'the probe'}"
    ch2 = hint.get("ch2")
    if ch2 and kind == "delay":
        wiring += f", CH{ch2} on {p2 or hint.get('probe2') or 'the second probe'}"
    if hint.get("probe") and not p1:
        wiring += f" [role {hint['probe']!r} not in the pin plan]"
    steps.append({"kind": "note", "text": f"{name}: {test['method'].rstrip('.')}. Wiring: {wiring}. "
                                          f"Pass: {crit} ({test['criterion']})"})
    cmd = f"cnlp measure {kind} --ch {ch}"
    if ch2 and kind == "delay":
        cmd += f" --ch2 {int(ch2)}"
    cmd += f" --criterion {_q(str(crit))} --record . {req}"
    note = " -> ".join(x for x in (p1, p2) if x) if kind == "delay" else (p1 or "")
    if note:
        cmd += f" --source-note {_q(note)}"
    steps.append({"kind": "command", "text": "measure and record", "command": cmd})
    steps.append({"kind": "command", "text": "manual fallback (no scope link)",
                  "command": _manual_add(req, "measured_scope", str(crit))})
    if hint.get("basis"):
        steps.append({"kind": "note", "text": f"criterion basis: {hint['basis']}"})
    return steps


# ---- key-matrix ghosting ----------------------------------------------------------

def _matrix(pins: dict[str, int]) -> tuple[list[int], list[int]]:
    rows = sorted(int(m.group(1)) for r in pins if (m := _ROW.match(r)))
    cols = sorted(int(m.group(1)) for r in pins if (m := _COL.match(r)))
    return rows, cols


def ghosting(rows: list[int], cols: list[int], layout: list[list[str]] | None = None) -> dict:
    """Minimal ghosting set: per 2x2 rectangle, the 4 'three corners pressed' combos."""
    def key(r: int, c: int) -> str:
        return f"R{r}C{c}"

    def name(r: int, c: int) -> str | None:
        try:
            return str(layout[rows.index(r)][cols.index(c)]) if layout else None
        except (IndexError, ValueError):
            return None

    def label(r: int, c: int) -> str:
        n = name(r, c)
        return f"{key(r, c)} ({n})" if n else key(r, c)

    singles = [{"id": f"K{i}", "key": key(r, c), "label": label(r, c)}
               for i, (r, c) in enumerate(itertools.product(rows, cols), 1)]
    combos = []
    for r1, r2 in itertools.combinations(rows, 2):
        for c1, c2 in itertools.combinations(cols, 2):
            corners = [(r1, c1), (r1, c2), (r2, c1), (r2, c2)]
            for ph in corners:
                held = [x for x in corners if x != ph]
                combos.append({"id": f"G{len(combos) + 1}",
                               "press": [key(*x) for x in held],
                               "press_labels": [label(*x) for x in held],
                               "expect": "only these 3 keys",
                               "phantom": key(*ph), "phantom_label": label(*ph)})
    return {"rows": rows, "cols": cols, "singles": singles, "combos": combos}


def _ghost_steps(req: str, hint: dict, pins: dict[str, int], etype: str) -> tuple[list[dict], dict | None]:
    rows, cols = _matrix(pins)
    if len(rows) < 2 or len(cols) < 2:
        return [{"kind": "note", "text": "ghosting generator needs at least 2 row_N and 2 col_N "
                                         "roles in the pin plan; found "
                                         f"{len(rows)} rows, {len(cols)} columns"}], None
    layout = hint.get("layout")
    g = ghosting(rows, cols, layout)
    ns, nc = len(g["singles"]), len(g["combos"])
    steps = [
        {"kind": "checklist", "text": f"Single-key pass: press each of the {ns} keys alone; "
                                      "exactly that key must read (K1..K%d)" % ns},
        {"kind": "checklist", "text": f"Ghosting set: {nc} three-corner combos "
                                      f"({len(list(itertools.combinations(rows, 2)))} row pairs x "
                                      f"{len(list(itertools.combinations(cols, 2)))} column pairs x 4). "
                                      "Hold the three keys; the reading must be only those three, "
                                      "and the listed phantom must NOT appear (G1..G%d)" % nc},
        {"kind": "note", "text": "Only a rectangle's three corners can ghost, so these cover every "
                                 "3-key combination that could produce a phantom; other triples "
                                 "(not an L shape) cannot."},
        {"kind": "command", "text": "when every item passes",
         "command": f'cnlp evidence add . {req} {etype} pass --value {ns + nc} --unit checks '
                    f'--note "{ns} single keys + {nc} corner combos, no phantom"'},
        {"kind": "command", "text": "if any phantom appeared",
         "command": f'cnlp evidence add . {req} {etype} fail --note "phantom <key> with <3 keys>"'},
    ]
    if not layout:
        steps.insert(2, {"kind": "note", "text": "no key layout in requirements.yaml (verify.layout); "
                                                 "keys are shown as grid positions only"})
    return steps, g


# ---- plan -------------------------------------------------------------------------

def _row_plan(row: dict, meta: dict, tests: dict[str, dict], pins: dict[str, int],
              notes: list[str]) -> dict:
    req = row["req"]
    accepts = list(row["accepts"])
    hint = (meta or {}).get("verify") or {}
    mine = [t for t in tests.values() if req in t["covers"]]
    thints = hint.get("tests") or {}
    for tn in thints:
        if tn not in tests:
            notes.append(f"{req}: verify hint for test {tn!r}, but the spec has no such test")
        elif req not in tests[tn]["covers"]:
            notes.append(f"{req}: verify hint for test {tn!r}, which does not cover {req}")
    steps: list[dict] = []
    checklist = None
    if hint.get("generator") == "ghosting":
        et = _evidence_type(accepts, hint, False)
        steps, checklist = _ghost_steps(req, hint, pins, et)
    for t in mine:
        if hint.get("generator") == "ghosting":
            break
        th = thints.get(t["name"]) or {}
        if th.get("measure"):
            steps += _measure_steps(req, t, th, accepts, pins)
            continue
        et = _evidence_type(accepts, th, False)
        steps.append({"kind": "note", "text": f"{t['name']}: {t['method']} Pass: {t['criterion']}"})
        if not th and _SCOPE_WORDS.search(t["method"]) and "measured_scope" in accepts:
            steps.append({"kind": "note", "text": f"{t['name']}: measurable on the scope but "
                                                  "requirements.yaml has no verify hint (measure kind, "
                                                  "probe, criterion), so no ready measure command"})
            et = "measured_scope"
        if th.get("note"):
            steps.append({"kind": "note", "text": th["note"]})
        steps.append({"kind": "command", "text": "record the result",
                      "command": _manual_add(req, et, None, th)})
    if not mine and not steps:
        et = _evidence_type(accepts, hint, False)
        steps = [{"kind": "note", "text": "no test in the spec's Verification plan covers this "
                                          f"requirement; verify it by hand ({'/'.join(accepts) or 'no evidence type accepted'})"},
                 {"kind": "command", "text": "record the result", "command": _manual_add(req, et, None)}]
    out = {"req": req, "text": row["text"], "kind": row["kind"],
           "tests": [{"name": t["name"], "method": t["method"], "criterion": t["criterion"]}
                     for t in mine],
           "evidence_types": accepts, "status": row["status"], "steps": steps}
    if not mine:
        out["no_spec_test"] = True
    if checklist:
        out["checklist"] = checklist
    return out


def plan(cfg_path: Path, req: str | None = None) -> dict:
    cfg_path = Path(cfg_path).expanduser()
    acfg, rcfg, target, base = evidence._load_cfg(cfg_path)
    ev = evidence.build(cfg_path)
    notes: list[str] = []
    rows = ev["rows"]
    if req is not None:
        rows = [r for r in rows if r["req"] == req]
        if not rows:
            raise evidence.EvidenceError(
                f"unknown requirement {req!r}; known: {', '.join(r['req'] for r in ev['rows'])}")
    tests = _tests_table(rcfg, target)
    pins = _pins(acfg, base, notes)
    meta_all = rcfg.get("requirements") or {}
    reqs = [_row_plan(r, meta_all.get(r["req"]) or {}, tests, pins, notes) for r in rows]
    return {"requirements": reqs, "notes": notes,
            "no_spec_test": [r["req"] for r in reqs if r.get("no_spec_test")]}


# ---- output -----------------------------------------------------------------------

def _status(st: Any, s: str) -> str:
    color = {"pass": "green", "fail": "red", "stale": "yellow"}.get(s)
    return st.c(color, s) if color else s


def format_text(result: dict, st: Any) -> str:
    lines: list[str] = []
    for r in result["requirements"]:
        lines.append(f"{st.c('bold', r['req'])}  {r['text']}  [{_status(st, r['status'])}]")
        if r["tests"]:
            lines.append("  tests: " + "; ".join(t["name"] for t in r["tests"]))
        lines.append("  accepts: " + (", ".join(r["evidence_types"]) or "nothing"))
        n = 0
        for s in r["steps"]:
            if s["kind"] == "command":
                lines.append(f"    {s['text']}:")
                lines.append(f"      $ {s['command']}")
            else:
                n += 1
                lines.append(f"  {n}. {s['text']}")
        g = r.get("checklist")
        if g:
            lines.append(f"  first {_PREVIEW} of {len(g['combos'])} combos (--markdown for the full list):")
            for c in g["combos"][:_PREVIEW]:
                lines.append(f"    {c['id']:<5} press {' + '.join(c['press_labels'])}; "
                             f"expect only those; watch {c['phantom_label']}")
        lines.append("")
    if result["no_spec_test"]:
        lines.append("No verification-plan test in the spec for: " + ", ".join(result["no_spec_test"])
                     + " (verify by hand; record tested/reviewed evidence).")
    for n in result["notes"]:
        lines.append(f"{st.c('yellow', st.warn_sym)} {n}")
    return "\n".join(lines).rstrip() + "\n"


def to_markdown(result: dict) -> str:
    out = ["# Verification checklist", ""]
    for r in result["requirements"]:
        out += [f"## {r['req']}: {r['text']}", "",
                f"Status: {r['status']}. Accepted evidence: {', '.join(r['evidence_types']) or 'none'}.", ""]
        for t in r["tests"]:
            out.append(f"- Test **{t['name']}**: {t['method']} Pass: {t['criterion']}")
        if r["tests"]:
            out.append("")
        for s in r["steps"]:
            if s["kind"] == "command":
                out += [f"- [ ] {s['text']}:", "", "  ```", f"  {s['command']}", "  ```"]
            else:
                out.append(f"- [ ] {s['text']}")
        out.append("")
        g = r.get("checklist")
        if g:
            out += ["### Single keys", ""]
            out += [f"- [ ] {k['id']} {k['label']}" for k in g["singles"]]
            out += ["", f"### Three-corner combos ({len(g['combos'])})", ""]
            out += [f"- [ ] {c['id']}: press {' + '.join(c['press_labels'])}; expect only those; "
                    f"watch for phantom {c['phantom_label']}" for c in g["combos"]]
            out.append("")
    if result["no_spec_test"]:
        out += ["No verification-plan test in the spec for: " + ", ".join(result["no_spec_test"]) + ".", ""]
    out += [f"Note: {n}" for n in result["notes"]]
    return "\n".join(out).rstrip() + "\n"
