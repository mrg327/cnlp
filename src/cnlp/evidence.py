"""Evidence matrix: what actually backs each requirement in the target's spec.

Read-only on the target. Requirements (`R1`, `F10`, ...) and the verification
plan's "Covers" column are parsed at runtime from the target's spec markdown
(`spec:` in `projects/<name>/requirements.yaml`, default `docs/spec.md`); the
sentences are never copied into cnlp. `requirements.yaml` adds per-ID metadata
(`kind`, `accepts`, `static_checks`). Records the user measured live in
`projects/<name>/evidence.jsonl` (append-only, written by `add`).

Evidence types, strongest first (`STRENGTH`):
  measured_scope  external measurement (scope); strongest for timing
  tested          on-target or host test
  measured_serial esp_timer/counters over serial; weaker than a scope
  static          lint/audit clean (added automatically from `static_checks`)
  reviewed        user inspection or listening ("no audible clicks")
  none
`accepted_unverified` (requirements.yaml) is a flag, not evidence.

Row status: fail if the newest record of any type is a fail; else pass if a
passing record's type is in the requirement's `accepts`; else accepted (flag);
else missing. `strength` is the strongest passing type, even if not accepted
(so a timing requirement with only static support reads strength=static,
status=missing). Stale: a passing, accepting record carrying `synth_commit`
that differs from the target's current HEAD commit (read from .git files) is
stale; records without a commit are never stale. Automatic static records are
always current (they cite the audit snapshot sha). Missing evidence is normal
mid-project and does not make `ok` false; only fail rows do.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from cnlp import audit as audit_mod

STRENGTH = ["measured_scope", "tested", "measured_serial", "static", "reviewed", "none"]
TYPES = set(STRENGTH) - {"none"}
RESULTS = {"pass", "fail"}
ID_RE = re.compile(r"^[A-Z]{1,3}\d+$")


class EvidenceError(Exception):
    pass


def _load_cfg(cfg_path: Path) -> tuple[dict, dict, Path, Path]:
    cfg_path = Path(cfg_path).expanduser()
    acfg = yaml.safe_load(cfg_path.read_text("utf-8")) or {}
    req_file = cfg_path.parent / "requirements.yaml"
    if not req_file.is_file():
        raise EvidenceError(f"missing {req_file}")
    rcfg = yaml.safe_load(req_file.read_text("utf-8")) or {}
    target = Path(str(acfg["target"])).expanduser()
    return acfg, rcfg, target, cfg_path.parent


def _cells(line: str) -> list[str]:
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return []
    return [c.strip() for c in re.split(r"(?<!\\)\|", s.strip("|"))]


def _tables(md: str):
    """Yield (header_cells, rows) for each markdown table."""
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        head = _cells(lines[i])
        if head and i + 1 < len(lines) and re.fullmatch(r"[\s|:-]+", lines[i + 1]) and "-" in lines[i + 1]:
            rows, j = [], i + 2
            while j < len(lines) and _cells(lines[j]):
                rows.append(_cells(lines[j]))
                j += 1
            yield head, rows
            i = j
        else:
            i += 1


def parse_spec(md: str) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """-> ({id: {text, target?}}, {id: [verification test names covering it]})."""
    reqs: dict[str, dict] = {}
    covered: dict[str, list[str]] = {}
    for head, rows in _tables(md):
        low = [h.lower() for h in head]
        if "test" in low and "covers" in low:
            ci, ti = low.index("covers"), low.index("test")
            for r in rows:
                if len(r) > ci:
                    for rid in re.findall(r"[A-Z]{1,3}\d+", r[ci]):
                        covered.setdefault(rid, []).append(r[ti])
        elif low and low[0] == "id":
            for r in rows:
                if len(r) >= 2 and ID_RE.match(r[0]):
                    d = {"text": r[1].replace("\\~", "~")}
                    if len(r) > 2 and "target" in low:
                        d["target"] = r[2]
                    reqs[r[0]] = d
    return reqs, covered


def _snapshot_file(target: Path, rel: str, dest: Path) -> Path | None:
    """Copy one file from the target into dest (same relative path); never read it in place."""
    src = target / rel
    if not src.is_file():
        return None
    out = dest / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)
    return out


def _target_commit(target: Path) -> str | None:
    """Current HEAD commit from a temp snapshot of the .git ref files (no git subprocess)."""
    with tempfile.TemporaryDirectory(prefix="cnlp-evidence-") as tmp:
        snap = Path(tmp)
        _snapshot_file(target, ".git/HEAD", snap)
        head_file = snap / ".git" / "HEAD"
        if head_file.is_file() and head_file.read_text("utf-8").startswith("ref:"):
            ref = head_file.read_text("utf-8").split(None, 1)[1].strip()
            _snapshot_file(target, f".git/{ref}", snap)
            _snapshot_file(target, ".git/packed-refs", snap)
        return _read_commit(snap)


def _read_commit(target: Path) -> str | None:
    try:
        head = (target / ".git" / "HEAD").read_text("utf-8").strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head.split(None, 1)[1]
        f = target / ".git" / ref
        if f.is_file():
            return f.read_text("utf-8").strip()
        packed = target / ".git" / "packed-refs"
        if packed.is_file():
            for ln in packed.read_text("utf-8").splitlines():
                if ln.endswith(" " + ref):
                    return ln.split()[0]
    except OSError:
        pass
    return None


def _evidence_path(base: Path, rcfg: dict) -> Path:
    return base / str(rcfg.get("evidence", "evidence.jsonl"))


def _read_records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for n, ln in enumerate(path.read_text("utf-8").splitlines(), 1):
        if ln.strip():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError as e:
                raise EvidenceError(f"{path}:{n}: bad JSON: {e}") from e
    return out


def _spec_for(rcfg: dict, target: Path) -> tuple[dict, dict]:
    rel = str(rcfg.get("spec", "docs/spec.md"))
    with tempfile.TemporaryDirectory(prefix="cnlp-evidence-") as tmp:
        spec = _snapshot_file(target, rel, Path(tmp))  # parse a copy, never the target in place
        if spec is None:
            raise EvidenceError(f"spec not found: {target / rel}")
        return parse_spec(spec.read_text("utf-8"))


def add(cfg_path: Path, req: str, type: str, result: str, *, value=None, unit=None,
        criterion=None, source=None, note: str = "", synth_commit: str | None = None) -> dict:
    """Append one user-recorded evidence record to projects/<name>/evidence.jsonl."""
    _, rcfg, target, base = _load_cfg(cfg_path)
    reqs, _ = _spec_for(rcfg, target)
    if req not in reqs:
        raise EvidenceError(f"unknown requirement {req!r}; known: {', '.join(sorted(reqs))}")
    if type not in TYPES:
        raise EvidenceError(f"unknown evidence type {type!r}; allowed: {', '.join(sorted(TYPES))}")
    if result not in RESULTS:
        raise EvidenceError("result must be 'pass' or 'fail'")
    rec: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "req": req,
                           "type": type, "result": result}
    for k, v in (("value", value), ("unit", unit), ("criterion", criterion), ("source", source)):
        if v is not None:
            rec[k] = v
    rec["note"] = note
    commit = synth_commit or _target_commit(target)
    if commit:
        rec["synth_commit"] = commit
    rec["recorded_by"] = "user"
    out = audit_mod.guard_write(_evidence_path(base, rcfg), target)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def _static_records(rcfg: dict, result: dict) -> list[dict]:
    fired = set((result.get("summary") or {}).get("by_rule") or {})
    for key in ("plan_findings", "audit_findings", "lint_findings"):
        fired |= {f["rule"] for f in result.get(key, [])}
    sha = (result.get("snapshot") or {}).get("sha256", "")[:12]
    recs = []
    for rid, meta in (rcfg.get("requirements") or {}).items():
        checks = list((meta or {}).get("static_checks") or [])
        if not checks:
            continue
        hit = sorted(set(checks) & fired)
        recs.append({"req": rid, "type": "static", "result": "fail" if hit else "pass",
                     "criterion": "no " + "/".join(checks),
                     "source": f"audit snapshot {sha}", "auto": True,
                     "note": ("fired: " + ", ".join(hit)) if hit else "clean",
                     "recorded_by": "cnlp"})
    return recs


def _row(rid: str, info: dict, meta: dict, recs: list[dict], accepted: bool,
         covered_by: list[str], head: str | None) -> dict:
    accepts = set(meta.get("accepts") or [])
    latest: dict[str, dict] = {}
    for r in recs:  # file order == time order; newest per type wins
        latest[r["type"]] = r
    passing = [r for r in latest.values() if r["result"] == "pass"]
    failing = [r for r in latest.values() if r["result"] == "fail"]
    strength = next((t for t in STRENGTH if any(r["type"] == t for r in passing)), "none")
    good = [r for r in passing if r["type"] in accepts]
    if failing:
        status = "fail"
    elif good:
        stale = all(r.get("synth_commit") and head and r["synth_commit"] != head for r in good)
        status = "stale" if stale else "pass"
    elif accepted:
        status = "accepted"
    else:
        status = "missing"
    return {"req": rid, "text": info["text"], "kind": meta.get("kind", ""),
            "strength": strength, "status": status, "evidence": recs,
            "covered_by": covered_by, "accepts": sorted(accepts, key=STRENGTH.index)}


def build(cfg_path: Path) -> dict:
    _acfg, rcfg, target, base = _load_cfg(cfg_path)
    reqs, covered = _spec_for(rcfg, target)
    notes: list[str] = []
    meta_all = rcfg.get("requirements") or {}
    for rid in meta_all:
        if rid not in reqs:
            notes.append(f"requirements.yaml lists {rid} but the spec has no such requirement")
    for rid in reqs:
        if rid not in meta_all:
            notes.append(f"{rid} has no metadata in requirements.yaml (nothing accepted as evidence)")
    try:
        res = audit_mod.audit(Path(cfg_path))
        snapshot = res.get("snapshot", {})
        auto = _static_records(rcfg, res)
        notes += list(res.get("notes", []))
    except (audit_mod.AuditError, OSError, KeyError, ValueError) as e:  # audit failure must not hide user-recorded evidence
        snapshot, auto = {}, []
        notes.append(f"static evidence unavailable: audit failed: {e}")
    records = _read_records(_evidence_path(base, rcfg))
    head = _target_commit(target)
    accepted = set(rcfg.get("accepted_unverified") or [])
    rows = []
    for rid, info in reqs.items():
        recs = [r for r in records if r.get("req") == rid] + [r for r in auto if r["req"] == rid]
        rows.append(_row(rid, info, meta_all.get(rid) or {}, recs, rid in accepted,
                         covered.get(rid, []), head))
    summary = {"total": len(rows)}
    for s in ("pass", "fail", "missing", "stale", "accepted"):
        summary[s] = sum(r["status"] == s for r in rows)
    snapshot = dict(snapshot, synth_commit=head)
    return {"ok": summary["fail"] == 0, "target": str(target), "snapshot": snapshot,
            "rows": rows, "summary": summary, "notes": notes}
