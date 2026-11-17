"""Approval gates and the append-only decision log.

Gates are ordered: intent, hardware_risk, flash (interface gate: TODO, out of MVP scope).
A gate's hash is sha256 of the exact summary text shown. An approval is a log line whose
shown_sha256 equals the gate's current hash, so any upstream change re-opens the gate.

Flash broker API: `approved_image(project, image_hash(project))` and `image_hash(project)`.
`approved_image` recomputes the flash summary for the image now on disk and checks that an
approve entry with that summary hash exists. The summary contains `image sha256: <hash>`, so
a rebuilt image changes the summary hash and the old approval stops counting. No extra log
field is needed.

Log: `decisions.jsonl` (append-only, one JSON object per line); `decisions.md` is regenerated.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

GATE_ORDER = ["intent", "hardware_risk", "flash"]  # TODO: interface gate (post-MVP)
TITLES = {
    "intent": "Intent: is this the device you asked for?",
    "hardware_risk": "Hardware risk: warnings that need your sign-off",
    "flash": "Flash: put this image on the board",
}
CHOICES = ("approve", "change", "reject")
NEEDS_BUILD = "Build the project first (idf.py build)"


@dataclass
class _Gate:
    name: str
    summary: str
    blocked: bool = False
    reason: str | None = None
    skip: bool = False  # auto-satisfied, needs no approval
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.summary.encode("utf-8")).hexdigest()

    @property
    def id(self) -> str:
        return f"g-{self.hash[:8]}-{self.name}"


# ---- log ----

def _log_path(project: Path) -> Path:
    return Path(project) / "decisions.jsonl"


def _read_log(project: Path) -> list[dict[str, Any]]:
    p = _log_path(project)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def approved(project: Path, gate: str, hash: str) -> bool:
    return any(
        e.get("gate") == gate and e.get("choice") == "approve" and e.get("shown_sha256") == hash
        for e in _read_log(Path(project))
    )


def _cell(s: Any) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _write_md(project: Path) -> None:
    lines = [
        "# Decision log",
        "",
        "Generated from decisions.jsonl; do not edit.",
        "",
        "| Time | Gate | Choice | Note | Shown hash | Supersedes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for e in _read_log(project):
        lines.append(
            "| " + " | ".join([
                _cell(e.get("ts")), _cell(e.get("gate")), _cell(e.get("choice")),
                _cell(e.get("note")), _cell(str(e.get("shown_sha256", ""))[:12]),
                _cell(e.get("supersedes")),
            ]) + " |"
        )
    (Path(project) / "decisions.md").write_text("\n".join(lines) + "\n")


# ---- image ----

def _build_files(project: Path) -> list[Path] | None:
    build = Path(project) / "build"
    boot = build / "bootloader" / "bootloader.bin"
    part = build / "partition_table" / "partition-table.bin"
    app: Path | None = None
    desc = build / "project_description.json"
    if desc.exists():
        try:
            name = json.loads(desc.read_text()).get("app_bin")
        except (ValueError, AttributeError):
            name = None
        if name:
            app = build / name
    if app is None:
        bins = sorted(build.glob("*.bin")) if build.is_dir() else []
        if len(bins) == 1:
            app = bins[0]
    if app is None:
        return None
    files = [boot, part, app]
    if not all(f.is_file() for f in files):
        return None
    return sorted(files)


def image_hash(project: Path) -> str | None:
    """sha256 over (relative path NUL bytes) of bootloader, partition table and app bin."""
    files = _build_files(Path(project))
    if files is None:
        return None
    h = hashlib.sha256()
    for f in files:
        h.update(f.relative_to(project).as_posix().encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


# ---- gate construction ----

def _manifest(project: Path):
    from cnlp import manifest

    return manifest.load(Path(project) / ".cnlp" / "manifest.yaml")


def _intent(project: Path) -> _Gate:
    from cnlp import cnl

    m = _manifest(project)
    out = [cnl.render(m).rstrip(), "", "Assumptions (not stated in your request):"]
    spec = Path(project) / "spec.cnl"
    assumptions = cnl.assumptions(m, text=spec.read_text()) if spec.exists() else cnl.assumptions(m)
    if not assumptions:
        out.append("  none")
    for a in assumptions:
        out.append("  - " + (a if isinstance(a, str) else
                             a.get("question") or a.get("text") or a.get("message") or json.dumps(a, sort_keys=True)))
    return _Gate("intent", "\n".join(out) + "\n")


def _hardware_risk(project: Path) -> _Gate:
    from cnlp import rules

    findings = rules.check(_manifest(project))
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    def line(tag: str, f: Any) -> str:
        pin = f" GPIO {f.gpio}" if f.gpio is not None else ""
        return f"{tag}{pin}: {f.message}"

    lines = [line("W4", f) for f in warnings]
    if errors:
        text = "Errors (cannot be approved; fix the spec):\n"
        text += "\n".join(line("E", f) for f in errors)
        if lines:
            text += "\n\nWarnings:\n" + "\n".join(lines)
        return _Gate("hardware_risk", text + "\n", blocked=True,
                     reason="Rule errors cannot be approved; fix them first")
    if not warnings:
        return _Gate("hardware_risk", "No hardware warnings.\n", skip=True)
    return _Gate("hardware_risk", "\n".join(lines) + "\n")


def _flash(project: Path) -> _Gate:
    files = _build_files(Path(project))
    if files is None:
        return _Gate("flash", f"No build found. {NEEDS_BUILD}.\n", blocked=True, reason=NEEDS_BUILD)
    m = _manifest(project)
    out = [
        f"target: {m.chip} / {m.module}",
        "port: chosen when flashing",
        "files:",
        *[f"  - {f.relative_to(project).as_posix()} ({f.stat().st_size} bytes)" for f in files],
        f"image sha256: {image_hash(project)}",
    ]
    return _Gate("flash", "\n".join(out) + "\n")


_BUILDERS = {"intent": _intent, "hardware_risk": _hardware_risk, "flash": _flash}


def _pending(project: Path) -> _Gate | None:
    """First gate (in order) that is neither auto-satisfied nor approved for current content."""
    project = Path(project)
    for name in GATE_ORDER:
        g = _BUILDERS[name](project)
        if g.skip or (not g.blocked and approved(project, name, g.hash)):
            continue
        return g
    return None


def _public(g: _Gate, blocked: bool | None = None, reason: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": g.id, "name": g.name, "title": TITLES[g.name], "summary": g.summary,
        "hash": g.hash, "blocked": g.blocked if blocked is None else blocked,
    }
    r = reason or g.reason
    if r:
        d["reason"] = r
    return d


def next_gate(project: Path, only: str | None = None) -> dict[str, Any] | None:
    g = _pending(project)
    if g is None:
        return None
    if only is not None and only != g.name:
        if only not in GATE_ORDER or GATE_ORDER.index(only) < GATE_ORDER.index(g.name):
            return None  # asked-for gate is already satisfied
        return _public(g, blocked=True, reason=f"Approve the {g.name} gate first")
    return _public(g)


def answer(project: Path, gate_id: str, hash: str, choice: str, note: str = "") -> tuple[bool, str]:
    project = Path(project)
    if choice not in CHOICES:
        return False, f"Choice must be one of: {', '.join(CHOICES)}"
    g = _pending(project)
    if g is None:
        return False, "There is nothing waiting for approval"
    if gate_id != g.id:
        if gate_id.endswith("-" + g.name):
            return False, "What you reviewed has changed since; run `cnlp gate next` again"
        return False, f"That is not the gate waiting for you; the {g.name} gate comes first"
    if hash != g.hash:
        return False, "What you reviewed has changed since; run `cnlp gate next` again"
    if g.blocked:
        if g.name == "hardware_risk":
            return False, ("Approval can accept a warning but never an error; "
                           "fix the errors in the spec and try again")
        return False, g.reason or "This gate cannot be answered yet"
    prior = [e for e in _read_log(project) if e.get("gate") == g.name]
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "gate": g.name,
        "gate_id": g.id,
        "shown_sha256": g.hash,
        "choice": choice,
        "note": note,
        "supersedes": prior[-1]["gate_id"] if prior else None,
    }
    with _log_path(project).open("a") as f:
        f.write(json.dumps(entry) + "\n")
    _write_md(project)
    if choice == "approve":
        return True, f"Recorded approval of the {g.name} gate"
    return True, f"Recorded '{choice}' for the {g.name} gate; it is still open"


def approved_image(project: Path, image_hash_: str | None) -> bool:
    """True iff the flash summary for the image on disk now was approved and matches image_hash_."""
    project = Path(project)
    cur = image_hash(project)
    if cur is None or image_hash_ != cur:
        return False
    return approved(project, "flash", _flash(project).hash)
