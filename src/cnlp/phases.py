"""Parse a target's build procedure (markdown) into phases.

Read at runtime from a temp snapshot of the target's file; nothing is copied into cnlp.
Expected shape: a "Progress" table (`| Phase | Outcome | Status |`, first cell "N. Name")
and `## Phase N: Name` sections with `**Goal:**`, `**Check:**` and numbered steps.
GPIOs and requirement IDs are taken from a phase's own text, skipping the Background,
What-you-will-learn and Common-problems paragraphs (they talk about pins in general).
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from cnlp.evidence import _cells, _snapshot_file, _tables

DEFAULT_PROCEDURE = "docs/build-procedure.md"
SKIP_LABELS = ("background", "what you will learn", "common problems")
_PHASE_H = re.compile(r"^##\s+Phase\s+(\d+)\s*[:.\-–]\s*(.+?)\s*$")
_LABEL = re.compile(r"^\*\*([^*]+?)\*\*|^\*\*([^*]+?)[.:]\*\*")
_NUM = r"\d{1,2}"
_RANGE = rf"{_NUM}(?:\s*[–-]\s*{_NUM})?"
_GPIO_LIST = re.compile(
    rf"GPIOs?\s+({_RANGE}(?:\s*(?:,|and|or|/)\s*(?:and\s+|or\s+)?{_RANGE})*)(?![\w.]\d)", re.IGNORECASE)
_ARROW = re.compile(rf"(?:→|->)\s*(?:GPIO\s*)?({_NUM})(?!\w|\.\d)")
_REQ = re.compile(r"\b[RF]\d+\b")
_REQ_RANGE = re.compile(r"\b[RF]\d+\s*[–-]\s*[RF]?\d+\b")
_LABEL_USE = re.compile(r"\b[RF]\d+\b(?=\s+(?:is the\b|\((?:top|bottom)\b))|\b(?:rows?|columns?)\s+[RF]\d+\b")
_STATUS = {"done": "done", "complete": "done", "completed": "done",
           "in progress": "in_progress", "started": "in_progress", "wip": "in_progress",
           "not started": "not_started", "todo": "not_started", "planned": "not_started"}


def _status(word: str) -> str:
    return _STATUS.get(re.sub(r"[*_`]", "", word).strip().lower().rstrip("."), "unknown")


def _numbers(spec: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*(?:,|and|or|/)\s*", spec):
        m = re.fullmatch(rf"({_NUM})(?:\s*[–-]\s*({_NUM}))?", part.strip())
        if not m:
            continue
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a <= b <= 39:
            out += range(a, b + 1)
    return out


def _gpios(text: str) -> list[int]:
    found = [n for m in _GPIO_LIST.finditer(text) for n in _numbers(m.group(1))]
    found += [int(m.group(1)) for m in _ARROW.finditer(text) if int(m.group(1)) <= 39]
    return sorted(set(found))


def _reqs(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        if _cells(ln):  # table rows hold labels like R1 (top row), not requirements
            continue
        ln = _LABEL_USE.sub(" ", _REQ_RANGE.sub(" ", ln))
        for r in _REQ.findall(ln):
            if r not in out:
                out.append(r)
    return out


def _sections(md: str) -> dict[int, dict[str, Any]]:
    """phase number -> {name, goal, check, lines (kept)}."""
    secs: dict[int, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None
    skipping = False
    for ln in md.splitlines():
        h = _PHASE_H.match(ln)
        if h:
            cur = {"name": h.group(2), "goal": "", "check": "", "lines": []}
            secs[int(h.group(1))] = cur
            skipping = False
            continue
        if ln.startswith("## "):
            cur = None
            continue
        if cur is None:
            continue
        lab = _LABEL.match(ln)
        if lab:
            label = (lab.group(1) or lab.group(2) or "").strip().lower().rstrip(".:")
            skipping = label.startswith(SKIP_LABELS)
            for key in ("goal", "check"):
                if label == key:
                    cur[key] = re.sub(r"^\*\*[^*]+\*\*:?\s*", "", ln).strip()
        elif ln.startswith("## "):
            skipping = False
        if not skipping:
            cur["lines"].append(ln)
    return secs


def parse_procedure(md: str) -> dict[str, Any]:
    secs = _sections(md)
    phases: list[dict[str, Any]] = []
    for head, rows in _tables(md):
        low = [h.lower() for h in head]
        if len(low) >= 3 and low[0] == "phase" and "status" in low:
            oi, si = (low.index("outcome") if "outcome" in low else 1), low.index("status")
            for r in rows:
                m = re.match(r"(\d+)\s*[.:)\-]\s*(.+)$", r[0])
                if not m or len(r) <= max(oi, si):
                    continue
                n = int(m.group(1))
                sec = secs.get(n, {})
                body = sec.get("lines", [])
                phases.append({
                    "n": n, "name": m.group(2).strip(), "outcome": r[oi],
                    "status": _status(r[si]), "gpios": _gpios("\n".join(body)),
                    "reqs": _reqs(body), "goal": sec.get("goal", ""),
                    "check": sec.get("check", "")})
            break
    phases.sort(key=lambda p: p["n"])
    current = next((p for p in phases if p["status"] != "done"), None)
    return {"phases": phases, "current": current}


def load(target: Path, rel: str = DEFAULT_PROCEDURE) -> dict[str, Any] | None:
    """Parse `target/rel` from a temp copy; None if the file does not exist."""
    with tempfile.TemporaryDirectory(prefix="cnlp-phases-") as tmp:
        f = _snapshot_file(Path(target).expanduser(), rel, Path(tmp))
        return parse_procedure(f.read_text("utf-8")) if f else None


def earliest_for_gpio(phases: list[dict[str, Any]], gpio: int) -> dict[str, Any] | None:
    for p in phases:
        if gpio in p["gpios"]:
            return p
    return None
