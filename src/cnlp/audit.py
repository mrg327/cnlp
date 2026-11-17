"""Read-only audit of an existing ESP-IDF project against a controlled-English pin plan.

`audit(config_path)` reads `projects/<name>/audit.yaml`::

    target: ~/projects/engr844_final_project   # never written to
    sources: [main]                            # dirs under target
    spec: spec.cnl                             # relative to the yaml
    lint_profile: cnlp-profile.yaml            # optional, relative to the yaml

It parses the spec, checks the plan with the pin rules, statically extracts the
pins the C code configures (`cnlp.extract`), and diffs plan against code.

Rules: AUDIT_UNPLANNED, AUDIT_UNUSED ("not configured in code yet"; expected
while a project is mid-build), AUDIT_MODE, AUDIT_PULL, AUDIT_DRIVER,
AUDIT_CONFLICT, AUDIT_UNRESOLVED. Nothing is written inside the target;
`write_report` refuses such paths via `guard_write`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from cnlp import cnl, rules
from cnlp.extract import Extracted, Use, extract
from cnlp.manifest import Finding, Manifest, Pin

INPUT_ONLY = range(34, 40)
OUT_MODES = {"output", "input_output", "output_od", "input_output_od"}
IN_MODES = {"input", "input_output", "input_output_od"}
# driver signal -> (can drive, can read)
SIGNAL_DIR = {
    ("i2s", "bclk"): (True, False), ("i2s", "ws"): (True, False), ("i2s", "mclk"): (True, False),
    ("i2s", "clk"): (True, False), ("i2s", "dout"): (True, False), ("i2s", "din"): (False, True),
    ("uart", "tx"): (True, False), ("uart", "rx"): (False, True),
    ("uart", "rts"): (True, False), ("uart", "cts"): (False, True),
}
I2S_ALIASES = {
    "bclk": ("bclk", "bck", "sck"),
    "ws": ("ws", "lrc", "lrck", "lrclk", "wclk"),
    "dout": ("dout", "din", "data", "sd"),  # a plan names the amp's DIN pin
    "din": ("din", "sdin", "data", "sd"),
    "mclk": ("mclk", "mck"),
    "clk": ("clk", "bclk"),
}
PLAN_ROLE_FOR = {"ledc": {"pwm"}, "spi": {"spi"}, "uart": {"uart"}}


class AuditError(Exception):
    pass


def guard_write(path: Path, target: Path) -> Path:
    """Raise if `path` resolves inside `target`; return the resolved path otherwise."""
    p = Path(path).expanduser().resolve()
    t = Path(target).expanduser().resolve()
    if p == t or t in p.parents:
        raise AuditError(f"refusing to write inside the audited project: {p}")
    return p


def _loc(u: Use) -> str:
    return f"{u.file}:{u.line}"


def _describe(u: Use) -> str:
    if u.kind == "driver":
        return f"{u.driver} {u.signal}"
    if u.mode:
        return f"{u.kind} mode {u.mode}"
    return u.kind


def _modes(u: Use) -> tuple[bool, bool]:
    """(can drive, can read) implied by one use; (False, False) if it says nothing."""
    if u.kind in ("gpio_config", "set_direction") and u.mode and u.mode != "disable":
        return u.mode in OUT_MODES, u.mode in IN_MODES
    if u.kind == "driver":
        if u.driver == "i2c":
            return True, True
        if u.driver in ("ledc", "rmt") and u.signal != "rx":
            return True, False
        if u.driver == "pcnt":
            return False, True
        if u.driver == "spi":
            return {"mosi": (True, False), "sclk": (True, False), "cs": (True, False),
                    "miso": (False, True)}.get(u.signal or "", (True, True))
        return SIGNAL_DIR.get((u.driver, u.signal), (False, False))  # type: ignore[arg-type]
    return False, False


def _driver_ok(u: Use, plan: Pin) -> str | None:
    """None if the plan pin is consistent with the driver use, else the reason."""
    role = plan.role.lower()
    if u.driver == "i2s":
        aliases = I2S_ALIASES.get(u.signal or "", (u.signal or "",))
        if "i2s" in role and any(a in role for a in aliases):
            return None
        return f"plan role is '{plan.role}' ({plan.function}), not an I2S {u.signal} pin"
    if u.driver == "i2c":
        if plan.function == "i2c" and (plan.bus or "").endswith(u.signal or "?"):
            return None
        return f"plan role is '{plan.role}' ({plan.function}), not I2C {u.signal}"
    if u.driver == "pcnt":
        if plan.mode == "input":
            return None
        return f"plan role is '{plan.role}' with mode {plan.mode}, but PCNT reads this pin"
    if u.driver == "ledc":
        if plan.function == "pwm" or plan.mode == "output":
            return None
        return f"plan role is '{plan.role}' ({plan.function}), not an output/pwm pin"
    want = PLAN_ROLE_FOR.get(u.driver or "")
    if want is not None and plan.function in want:
        return None
    if u.driver == "uart" and ("uart" in role or "console" in role):
        return None
    if u.driver in ("spi", "rmt", "uart"):
        return f"plan role is '{plan.role}' ({plan.function}), not a {u.driver} {u.signal} pin"
    return None


def _diff(plan: Manifest, ex: Extracted) -> list[Finding]:
    out: list[Finding] = []
    by_pin = {p.gpio: p for p in plan.pins}

    for gpio in sorted(ex.pins):
        uses = ex.pins[gpio]
        p = by_pin.get(gpio)
        if p is None:
            sites = "; ".join(f"{_loc(u)} ({_describe(u)})" for u in uses[:4])
            more = f" (+{len(uses) - 4} more)" if len(uses) > 4 else ""
            out.append(Finding(
                "AUDIT_UNPLANNED", "warning",
                f"{_loc(uses[0])}: GPIO {gpio} is configured in code but the spec doesn't "
                f"mention it: {sites}{more}. Add it to the plan or remove the code.", gpio))
            continue
        out += _check_pin(p, uses)
        out += _check_conflict(p, uses)

    for p in plan.pins:
        if p.gpio not in ex.pins:
            out.append(Finding(
                "AUDIT_UNUSED", "warning",
                f"GPIO {p.gpio} ({p.role}): planned but not configured in code yet. "
                "It may be set up by a driver this extractor can't see, or not implemented yet.",
                p.gpio))
    for u in ex.unresolved:
        out.append(Finding("AUDIT_UNRESOLVED", "warning",
                           f"{u.file}:{u.line}: {u.what}: `{u.text}`. Not guessed; check by hand.",
                           None))
    return out


def _check_pin(p: Pin, uses: list[Use]) -> list[Finding]:
    out: list[Finding] = []
    g = p.gpio
    for u in uses:
        # mode
        if u.kind in ("gpio_config", "set_direction") and u.mode and u.mode != "disable":
            drive, read = _modes(u)
            if g in INPUT_ONLY and drive:
                out.append(Finding(
                    "AUDIT_MODE", "error",
                    f"{_loc(u)}: GPIO {g} ({p.role}) is input-only on the ESP32 but the code "
                    f"sets mode {u.mode}. Use GPIO_MODE_INPUT.", g))
            elif p.mode == "output" and not drive:
                out.append(Finding(
                    "AUDIT_MODE", "error",
                    f"{_loc(u)}: GPIO {g} ({p.role}) is planned as an output but the code sets "
                    f"mode {u.mode}.", g))
            elif p.mode == "input" and drive and not read:
                out.append(Finding(
                    "AUDIT_MODE", "error",
                    f"{_loc(u)}: GPIO {g} ({p.role}) is planned as an input but the code sets "
                    f"mode {u.mode}.", g))
        # driver
        if u.kind == "driver":
            why = _driver_ok(u, p)
            if why:
                out.append(Finding(
                    "AUDIT_DRIVER", "error",
                    f"{_loc(u)}: {u.driver} {u.signal} is mapped to GPIO {g}, but {why}.", g))
    out += _check_pull(p, uses)
    return out


def _check_pull(p: Pin, uses: list[Use]) -> list[Finding]:
    g = p.gpio
    out: list[Finding] = []
    ups = [u for u in uses if u.pull_up is True]
    downs = [u for u in uses if u.pull_down is True]
    if g in INPUT_ONLY and (ups or downs):
        u = (ups or downs)[0]
        out.append(Finding(
            "AUDIT_PULL", "error",
            f"{_loc(u)}: GPIO {g} ({p.role}) has no internal pull resistors (GPIO 34-39), "
            "but the code enables one. It has no effect; use an external resistor.", g))
        return out
    if p.pull in ("none", "external_up", "external_down", "external_offboard"):
        for u in ups + downs:
            kind = "pull-up" if u in ups else "pull-down"
            out.append(Finding(
                "AUDIT_PULL", "error",
                f"{_loc(u)}: GPIO {g} ({p.role}) enables the internal {kind} but the plan says "
                f"'{p.pull}'. Internal and external pulls would fight or change the level.", g))
    elif p.pull == "internal_up" and not ups:
        known = [u for u in uses if u.pull_up is False]
        if known:
            out.append(Finding(
                "AUDIT_PULL", "error",
                f"{_loc(known[0])}: GPIO {g} ({p.role}) is planned with an internal pull-up but "
                "the code never enables it.", g))
    elif p.pull == "internal_down" and not downs:
        known = [u for u in uses if u.pull_down is False]
        if known:
            out.append(Finding(
                "AUDIT_PULL", "error",
                f"{_loc(known[0])}: GPIO {g} ({p.role}) is planned with an internal pull-down "
                "but the code never enables it.", g))
    return out


def _check_conflict(p: Pin, uses: list[Use]) -> list[Finding]:
    """Two sites in different functions that configure one pin incompatibly (first pair only)."""
    for i, a in enumerate(uses):
        for b in uses[i + 1:]:
            if (a.file, a.function) == (b.file, b.function) and a.kind != "driver" and b.kind != "driver":
                continue
            why = _incompatible(a, b)
            if why:
                return [Finding(
                    "AUDIT_CONFLICT", "error",
                    f"{_loc(a)}: GPIO {p.gpio} (plan role '{p.role}') is configured here "
                    f"({_describe(a)}) and at {_loc(b)} ({_describe(b)}): {why}. "
                    "Each pin needs one owner.", p.gpio)]
    return []


def _incompatible(a: Use, b: Use) -> str | None:
    if a.kind == "driver" or b.kind == "driver":
        if a.kind == "driver" and b.kind == "driver":
            if (a.driver, a.signal) == (b.driver, b.signal):
                return None
            return "two drivers claim the pin"
        g = a if a.kind != "driver" else b
        if g.kind in ("gpio_config", "set_direction") and g.mode and g.mode != "disable":
            return "a driver and a direct GPIO call both configure it"
        return None
    ma, mb = a.mode, b.mode
    if ma and mb and "disable" not in (ma, mb):
        (da, ra), (db, rb) = _modes(a), _modes(b)
        if (da and not ra and rb and not db) or (db and not rb and ra and not da):
            return "one site makes it an output and the other an input"
    if a.kind == "gpio_config" and b.kind == "gpio_config" and (
            a.pull_up != b.pull_up or a.pull_down != b.pull_down):
        return "the two sites set different pulls"
    return None


def _plan_findings(spec: Path) -> tuple[Manifest, list[Finding]]:
    m = cnl.parse(spec.read_text("utf-8"))
    return m, rules.check(m)


def _lint(target: Path, profile: Path | None, notes: list[str]) -> list[Finding]:
    from cnlp import lint

    try:
        if profile is not None and profile.is_file():
            return lint.run(target, profile_path=profile)
        return lint.run(target)
    except TypeError:
        notes.append("lint skipped: lint.run() has no profile_path parameter yet")
        return []


SNAPSHOT_IGNORE = shutil.ignore_patterns("build", "managed_components", ".git")


def _snapshot(target: Path, sources: list[str], dest: Path) -> dict[str, Any]:
    """Copy `target/<source>` dirs into `dest` (same relative layout); hash what was copied."""
    for s in sources:
        src = target / s
        if src.is_dir():
            shutil.copytree(src, dest / s, symlinks=True, ignore=SNAPSHOT_IGNORE)
        elif src.is_file():
            (dest / s).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / s, follow_symlinks=False)
    h = hashlib.sha256()
    files = sorted(p for p in dest.rglob("*") if p.is_file() and not p.is_symlink())
    for p in files:
        h.update(p.relative_to(dest).as_posix().encode() + b"\0")
        h.update(p.read_bytes() + b"\0")
    return {"source": str(target), "files": len(files), "sha256": h.hexdigest()}


def projects_root() -> Path:
    """cnlp's `projects/` folder (configs live in the cnlp repo, never in the target).

    `CNLP_PROJECTS_DIR` overrides it, e.g. to point tests at configs in a temp dir.
    """
    override = os.environ.get("CNLP_PROJECTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "projects"


def find_config(path: Path, root: Path | None = None) -> Path:
    """Resolve what the user typed to an audit.yaml.

    Accepts the yaml itself, a cnlp config folder (`projects/synth`), or the target
    project directory (e.g. `~/projects/engr844_final_project`), which is matched
    against each `projects/*/audit.yaml`'s `target`.
    """
    p = Path(path).expanduser()
    if p.is_file():
        return p
    if (p / "audit.yaml").is_file():
        return p / "audit.yaml"
    root = root or projects_root()
    want = p.resolve()
    for cfg in sorted(root.glob("*/audit.yaml")):
        target = (yaml.safe_load(cfg.read_text("utf-8")) or {}).get("target")
        if target and Path(str(target)).expanduser().resolve() == want:
            return cfg
    raise AuditError(
        f"No audit config for {want}. Create {root}/<name>/audit.yaml with "
        f"'target: {want}' (see projects/synth/audit.yaml).")


def audit(config_path: Path) -> dict[str, Any]:
    cfg_path = Path(config_path).expanduser()
    cfg = yaml.safe_load(cfg_path.read_text("utf-8")) or {}
    base = cfg_path.parent
    target = Path(str(cfg["target"])).expanduser()
    sources = list(cfg.get("sources") or ["main"])
    spec = base / str(cfg.get("spec", "spec.cnl"))
    notes: list[str] = []
    if not target.is_dir():
        raise AuditError(f"target directory not found: {target}")

    plan, plan_findings = _plan_findings(spec)
    profile = None
    if cfg.get("lint_profile"):
        profile = base / str(cfg["lint_profile"])
        if not profile.is_file():
            notes.append(f"lint profile {profile.name} not found; using default lint profile")
    # Analyze a private copy only; paths in findings are relative to its root, which
    # mirrors the target root, so messages read `main/scan.c:22`.
    with tempfile.TemporaryDirectory(prefix="cnlp-audit-") as tmp:
        snap_root = Path(tmp)
        snapshot = _snapshot(target, sources, snap_root)
        ex = extract(snap_root, sources)
        lint_findings = _lint(snap_root, profile, notes)
    audit_findings = _diff(plan, ex)

    allf = plan_findings + audit_findings + lint_findings
    counts = Counter(f.rule for f in allf)
    unused = counts.get("AUDIT_UNUSED", 0)
    summary = {
        "errors": sum(f.severity == "error" for f in allf),
        "warnings": sum(f.severity == "warning" for f in allf),
        "planned_pins": len(plan.pins),
        "pins_in_code": len(ex.pins),
        "not_configured_in_code_yet": unused,
        "files_scanned": len(ex.files),
        "by_rule": dict(sorted(counts.items())),
    }
    return {
        "ok": summary["errors"] == 0,
        "target": str(target),
        "snapshot": snapshot,
        "plan_findings": [f.to_dict() for f in plan_findings],
        "audit_findings": [f.to_dict() for f in audit_findings],
        "lint_findings": [f.to_dict() for f in lint_findings],
        "summary": summary,
        "notes": notes,
        "extracted": ex.to_dict(),
    }


def write_report(result: dict[str, Any], config_path: Path) -> Path:
    """Write `<project>/.cnlp/audit.json`, refusing any path inside the target."""
    out = Path(config_path).expanduser().parent / ".cnlp" / "audit.json"
    guard_write(out, Path(result["target"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    return out
