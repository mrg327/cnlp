"""`cnlp status`: where the project is in its build procedure, and what is safe to ignore.

Composes `audit.audit`, `evidence.build` and `phases.load`; read-only on the target.
Rules added here: STATUS_PHASE_DONE_PIN_UNUSED (a pin whose earliest phase is done
but which the code still doesn't configure).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from cnlp import audit as audit_mod
from cnlp import evidence as evidence_mod
from cnlp import phases as phases_mod

_ROLE = re.compile(r"^GPIO (\d+) \(([^)]*)\)")
_VERIF = re.compile(r"verif|evidence", re.IGNORECASE)
_STOP = {"that", "this", "with", "from", "when", "have", "each", "does", "their", "then",
         "than", "never", "every", "into", "only", "other", "which", "while", "will"}


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP}


def _brief(p: dict[str, Any] | None) -> dict[str, Any] | None:
    return {k: p[k] for k in ("n", "name", "status")} if p else None


def enrich_unused(audit_result: dict[str, Any], phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """AUDIT_UNUSED findings -> [{gpio, role, phase: {n,name,status}|None}]."""
    out = []
    for f in audit_result.get("audit_findings", []):
        if f.get("rule") != "AUDIT_UNUSED":
            continue
        m = _ROLE.match(f.get("message", ""))
        gpio = f.get("gpio", int(m.group(1)) if m else None)
        p = phases_mod.earliest_for_gpio(phases, gpio) if gpio is not None else None
        out.append({"gpio": gpio, "role": m.group(2) if m else "", "phase": _brief(p)})
    return out


def relevant_rows(rows: list[dict[str, Any]], phase: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Requirements the phase names, or whose text/test names overlap its goal and check."""
    if not phase:
        return []
    explicit = set(phase.get("reqs", []))
    words = _stems(" ".join([phase["name"], phase.get("outcome", ""), phase.get("goal", ""),
                             phase.get("check", "")]))
    out = []
    for r in rows:
        have = _stems(" ".join([r.get("text", "")] + list(r.get("covered_by", []))))
        if r["req"] in explicit or len(words & have) >= 3:
            out.append({"req": r["req"], "text": r.get("text", ""), "status": r["status"],
                        "strength": r.get("strength", ""),
                        "tests": list(r.get("covered_by", []))})
    return out


SECTIONS = ("plan", "audit", "lint", "param")
# Informational "not built yet" findings: expected mid-project, never counted as warnings.
NOT_YET = {"AUDIT_UNUSED", "PARAM_MISSING", "PARAM_TASK_MISSING"}


def status(cfg_path: Path) -> dict[str, Any]:
    cfg_path = Path(cfg_path).expanduser()
    cfg = yaml.safe_load(cfg_path.read_text("utf-8")) or {}
    target = Path(str(cfg["target"])).expanduser()
    res = audit_mod.audit(cfg_path)
    ev = evidence_mod.build(cfg_path)
    notes = list(res.get("notes", []))
    proc = phases_mod.load(target, str(cfg.get("procedure", phases_mod.DEFAULT_PROCEDURE)))
    plist = proc["phases"] if proc else []
    cur = proc["current"] if proc else None
    nxt = next((p for p in plist if cur and p["n"] > cur["n"]), None)
    if proc is None:
        notes.append("build procedure not found; phase information unavailable")
    elif not plist:
        notes.append("build procedure has no recognisable Progress table")

    unused = enrich_unused(res, plist)
    warnings: list[dict[str, Any]] = []
    for u in unused:
        p = u["phase"]
        if p and p["status"] == "done":
            warnings.append({
                "rule": "STATUS_PHASE_DONE_PIN_UNUSED", "severity": "warning", "gpio": u["gpio"],
                "message": f"GPIO {u['gpio']} ({u['role']}) belongs to phase {p['n']} {p['name']}, "
                           "which is marked done, but the code doesn't configure it."})
    allf = [f for k in SECTIONS for f in res.get(k + "_findings", [])]
    actionable_warnings = sum(1 for f in allf
                              if f.get("severity") == "warning" and f.get("rule") not in NOT_YET)
    rt = [f for f in res.get("lint_findings", []) if str(f.get("rule", "")).startswith("RT")]
    s = res["summary"]
    return {
        "ok": s["errors"] == 0 and ev["summary"]["fail"] == 0,
        "target": res["target"],
        "snapshot": res["snapshot"],
        "phase": {"current": _brief(cur) | {"outcome": cur["outcome"]} if cur else None,
                  "done_count": sum(p["status"] == "done" for p in plist),
                  "total": len(plist), "last": plist[-1]["n"] if plist else None},
        "audit": {"errors": s["errors"], "warnings": actionable_warnings,
                  "not_yet": sum(1 for f in allf if f.get("rule") in NOT_YET),
                  "by_section": {k: len(res.get(k + "_findings", [])) for k in SECTIONS},
                  "strapping": sorted({int(m.group(1)) for f in allf if f.get("severity") == "warning"
                                  for m in [re.search(r"GPIO (\d+)", f.get("message", ""))]
                                  if m and "strapping" in f.get("message", "").lower()})},
        "realtime": {"clean": not rt, "findings": rt},
        "unused_pins": unused,
        "status_findings": warnings,
        "evidence": {"summary": ev["summary"], "relevant": relevant_rows(ev["rows"], cur),
                     "current_phase_n": cur["n"] if cur else None,
                     "expected_phase": next((p["n"] for p in plist if _VERIF.search(p["name"])), None)},
        "next_check": cur["check"] if cur else "",
        "watch": list(cur.get("common_problems", [])) if cur else [],
        "next_phase": {"n": nxt["n"], "name": nxt["name"]} if nxt else None,
        "notes": notes + ev.get("notes", []),
    }
