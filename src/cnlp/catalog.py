"""Rules catalog: every rule ID cnlp can emit, with the user's review state.

Rules are discovered, not listed by hand: chip/module YAML rule tables, the emitted-ID
scan in `cnlp.knowledge`, the rtlint docstring, and the "Draft rule data" tables in
docs/design.md (for pins and one-line summaries, and for rules that are data only: C4,
C8, C16, W2). Review state lives in rules/review.yaml, which the user edits by hand (or
through their agent); cnlp only reads it and never marks anything reviewed.
"""

from __future__ import annotations

import re
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from cnlp import knowledge

FAMILIES = ("chip", "module", "cross-pin", "realtime", "audit", "lint", "param", "fsm", "status")
REVIEWABLE = {"chip", "module", "cross-pin", "realtime"}
STATUSES = ("unreviewed", "reviewed", "changes-requested")
CROSS_PIN = {"OWNER", "OPEN_DRAIN_PULL", "FLOATING", "I2C_WEAK_PULL"}
_OWNER_FILE = {"audit": "audit.py", "lint": "lint.py", "param": "params.py", "fsm": "fsm.py",
               "status": "status.py", "realtime": "rtlint.py"}
_PKG = Path(__file__).resolve().parent
_DESIGN = _PKG.parents[1] / "docs" / "design.md"
_CITE = "docs/design.md, Draft rule data"
_CHIP_MODULE = re.compile(r"^[CW]\d+$")


def _family(rid: str) -> str:
    if rid in CROSS_PIN:
        return "cross-pin"
    if re.match(r"RT\d", rid):
        return "realtime"
    if rid[0] == "C" and rid[1:].isdigit():
        return "chip"
    if rid[0] == "W" and rid[1:].isdigit():
        return "module"
    return {"AUDIT": "audit", "LINT": "lint", "PARAM": "param", "FSM": "fsm",
            "STATUS": "status"}.get(rid.split("_")[0], "status")


def _sev_text(s: set[str]) -> str:
    return "/".join(x for x in ("error", "warning", "info") if x in s)


def _design_rows() -> dict[str, dict[str, str]]:
    """{C1: {pins, summary, severity}} from the design.md rule tables (empty if absent)."""
    try:
        text = _DESIGN.read_text("utf-8")
    except OSError:
        return {}
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"^\|\s*([CW]\d+)\s*\|(.*?)\|(.*?)\|(.*?)\|\s*$", text, re.MULTILINE):
        rid, pins, rule, sev = (g.strip() for g in m.groups())
        out[rid] = {"pins": pins, "summary": rule, "severity": sev}
    return out


def _yaml_rules() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for layer in ("chip", "module"):
        for res in sorted((resources.files("cnlp") / "rules" / layer).iterdir(),
                          key=lambda r: r.name):
            if not res.name.endswith(".yaml"):
                continue
            text = res.read_text()
            data = yaml.safe_load(text)
            lines = text.splitlines()
            for key, r in data["rules"].items():
                rid = r.get("id", key)
                line = next((i + 1 for i, ln in enumerate(lines)
                             if ln.startswith(f"  {key}:")), 1)
                d = out.setdefault(rid, {"sev": set(), "msgs": [], "src": f"rules/{layer}/{res.name}:{line}"})
                d["sev"].add(r["severity"])
                d["msgs"].append(r["message"])
            for rid in data["pin_sets"].get("module_decides", []):
                out.setdefault(rid, {"sev": set(), "msgs": [], "src": f"rules/{layer}/{res.name}"})
    return out


def _rt_doc() -> dict[str, tuple[str, str]]:
    from cnlp import rtlint

    out = {}
    for m in re.finditer(r"^\s{2}(RT\d_[A-Z_0-9]+)\s+\((\w+)\)\s+(.+?)(?=\n\s{2}RT\d_|\n\n)",
                         rtlint.__doc__ or "", re.MULTILINE | re.DOTALL):
        out[m.group(1)] = (m.group(2), " ".join(m.group(3).split()))
    return out


def _scan_severity(rid: str, fam: str, fallback_file: str) -> set[str]:
    """Severities at the emit sites in the family's own module (handles err()/warn() helpers)."""
    path = _PKG / _OWNER_FILE.get(fam, fallback_file)
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return set()
    sev: set[str] = set()
    for i, ln in enumerate(lines):
        if f'"{rid}"' not in ln:
            continue
        window = " ".join(lines[i:i + 4])
        if re.search(r"\berr\(", ln):
            sev.add("error")
        if re.search(r"\bwarn\(", ln):
            sev.add("warning")
        for s in ("error", "warning"):
            if f'"{s}"' in window:
                sev.add(s)
    return sev


def _source_line(rid: str, fam: str, fallback_file: str) -> str:
    name = _OWNER_FILE.get(fam, fallback_file)
    for n in dict.fromkeys((name, fallback_file)):
        try:
            for i, ln in enumerate((_PKG / n).read_text("utf-8").splitlines(), 1):
                if f'"{rid}"' in ln:
                    return f"{n}:{i}"
        except OSError:
            pass
    return name


def _first_sentence(s: str) -> str:
    s = " ".join(s.split())
    s = s.replace("<...>", "…")
    return s if len(s) <= 160 else s[:157].rstrip() + "..."


def _load_review() -> dict[str, dict[str, Any]]:
    try:
        data = yaml.safe_load((resources.files("cnlp") / "rules" / "review.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return {}
    r = (data or {}).get("reviews") or {}
    return r if isinstance(r, dict) else {}


@cache
def _base() -> tuple[dict[str, Any], ...]:
    scan = knowledge._scan_emitted()
    ymap = _yaml_rules()
    design = _design_rows()
    rt = _rt_doc()
    blurbs = {**knowledge._AUDIT_RULES, **knowledge._LINT_RULES}
    ids = set(scan) | set(ymap) | set(design) | set(rt)
    rules: list[dict[str, Any]] = []
    for rid in ids:
        fam = _family(rid)
        y, d, s = ymap.get(rid), design.get(rid), scan.get(rid)
        file = s["file"] if s else "rules.py"
        sev: set[str] = set(y["sev"]) if y else set()
        if rid in rt:
            sev.add(rt[rid][0])
        if not sev and s and s["severity"]:
            sev.add(s["severity"])
        if not sev and fam not in ("chip", "module", "cross-pin"):
            sev = _scan_severity(rid, fam, file)
        if fam in ("chip", "module", "cross-pin"):
            src = y["src"] if y else f"docs/design.md ({rid}, data only)"
        elif rid in rt:
            src = _source_line(rid, fam, file) + " (docstring)"
        else:
            src = _source_line(rid, fam, file)
        if d and d["summary"]:
            summary = d["summary"]
        elif rid in rt:
            summary = rt[rid][1]
        elif rid in blurbs:
            summary = blurbs[rid]
        elif s and s["message"]:
            summary = s["message"]
        elif y and y["msgs"]:
            summary = re.sub(r"^GPIO \{gpio\}( \(\{role\}\))?:\s*", "", y["msgs"][0])
            summary = re.sub(r"\s*\(\{\w+\}\)|\s*\{\w+\}", "", summary).strip()
        else:
            summary = ""
        sevtxt = _sev_text(sev) or (d["severity"].lower() if d else "")
        rules.append({
            "id": rid, "family": fam, "severity": sevtxt,
            "summary": _first_sentence(summary),
            "pins": d["pins"] if d else "",
            "source": src,
            "citation": _CITE if fam in ("chip", "module") else "",
            "emitted": bool(s or y and y["msgs"] or rid in rt),
            "reviewable": fam in REVIEWABLE,
        })
    order = {f: i for i, f in enumerate(FAMILIES)}

    def key(r: dict) -> tuple:
        n = re.findall(r"\d+", r["id"])
        return (order[r["family"]], r["id"][0], int(n[0]) if n else 0, r["id"])

    return tuple(sorted(rules, key=key))


def rules() -> list[dict]:
    """Every rule cnlp can emit (plus data-only chip/module rules), with merged review state."""
    rev = _load_review()
    out = []
    for r in _base():
        r = dict(r)
        if r["reviewable"]:
            e = rev.get(r["id"]) or {}
            st = e.get("status") or "unreviewed"
            r["review"] = {"status": st if st in STATUSES else "unreviewed",
                           "by": e.get("by"), "date": str(e["date"]) if e.get("date") else None,
                           "note": e.get("note")}
        else:
            r["review"] = None
        out.append(r)
    return out


def _select(rs: list[dict], family: str | None, needs_review: bool) -> list[dict]:
    if family:
        rs = [r for r in rs if r["family"] == family]
    if needs_review:
        rs = [r for r in rs if r["reviewable"] and r["review"]["status"] != "reviewed"]
    return rs


def _cell(s: Any) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def format_text(rules: list[dict], style: str = "plain", family: str | None = None,
                needs_review: bool = False) -> str:
    """Compact table. style: 'plain' (aligned columns) or 'markdown'."""
    rs = _select(rules, family, needs_review)
    if not rs:
        return "No rules match."
    head = ("ID", "FAMILY", "SEVERITY", "PINS", "REVIEW", "SUMMARY")
    rows = [(r["id"], r["family"], r["severity"], r["pins"],
             r["review"]["status"] if r["review"] else "-", r["summary"]) for r in rs]
    if style in ("markdown", "md"):
        lines = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
        lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
        return "\n".join(lines)
    w = [max(len(str(x[i])) for x in [head, *rows]) for i in range(5)]
    return "\n".join("  ".join(str(c).ljust(w[i]) for i, c in enumerate(row[:5])) + "  " + row[5]
                     for row in [head, *rows]).rstrip()


_SHEET_HEAD = """# cnlp rule review sheet

Print this, tick the boxes and write notes in the margin. To record a decision, edit
`src/cnlp/rules/review.yaml` by hand (set `status` to `reviewed` or `changes-requested`,
plus `by`, `date`, `note`), or ask Claude to record it during a check-in. The model in
Pi is blocked from editing this file. cnlp only reads review.yaml;
it never marks a rule reviewed itself.

Data is drafted from Espressif's ESP-IDF GPIO reference and the ESP32 datasheet strapping
table (see `docs/design.md`, "Draft rule data").
"""


def review_sheet() -> str:
    rs = [r for r in rules() if r["reviewable"]]
    out = [_SHEET_HEAD]
    titles = {"chip": "Chip rules (ESP32)", "module": "Module rules (ESP32-WROOM-32)",
              "cross-pin": "Cross-pin rules", "realtime": "Real-time rules (RT1-RT7)"}
    for fam in ("chip", "module", "cross-pin", "realtime"):
        fr = [r for r in rs if r["family"] == fam]
        out += [f"\n## {titles[fam]}\n",
                "| Done | ID | Rule | Severity | Pins | Source | Citation | Status | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in fr:
            st = r["review"]["status"]
            note = r["review"]["note"] or ""
            out.append("| [{}] | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                "x" if st == "reviewed" else " ", r["id"], _cell(r["summary"]),
                _cell(r["severity"]), _cell(r["pins"]), _cell(r["source"]),
                _cell(r["citation"]), st, _cell(note)))
    return "\n".join(out) + "\n"
