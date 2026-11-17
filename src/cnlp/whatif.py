"""What-if pin checks: apply proposed controlled-English statements to the plan, run the rules.

Works on the project's spec.cnl held in the cnlp config folder (never the target project).
A pin statement whose role already exists on another GPIO is a *move*: the old pin is dropped.
Otherwise it adds a pin, or replaces the pin on the same GPIO with the same role.
Nothing is written; the result lists what the change would introduce or resolve.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from cnlp import cnl, rules
from cnlp.manifest import Finding, Manifest


class WhatIfError(ValueError):
    pass


def _spec_path(cfg_path: Path) -> Path:
    cfg = yaml.safe_load(Path(cfg_path).read_text("utf-8")) or {}
    return Path(cfg_path).parent / str(cfg.get("spec", "spec.cnl"))


def _key(f: Finding) -> tuple:
    return (f.rule, f.gpio, f.message)


def apply(base: Manifest, statements: str) -> tuple[Manifest, list[str]]:
    """Return the changed manifest and plain-English notes on what was applied."""
    header = cnl.render(replace(base, pins=(), state_machines=()))
    try:
        proposed = cnl.parse(header + statements.strip().rstrip(".") + ".\n")
    except cnl.CnlError as e:
        raise WhatIfError("; ".join(x["message"] for x in e.to_list())) from None
    pins, notes = list(base.pins), []
    for new in proposed.pins:
        old = next((p for p in pins if p.role == new.role and p.gpio != new.gpio), None)
        if old:
            pins.remove(old)
            notes.append(f"moves {new.role} from GPIO {old.gpio} to GPIO {new.gpio}")
        same = [p for p in pins if p.gpio == new.gpio and p.role == new.role]
        for p in same:
            pins.remove(p)
            notes.append(f"changes GPIO {new.gpio} ({new.role})")
        if not old and not same:
            notes.append(f"adds GPIO {new.gpio} ({new.role})")
        pins.append(new)
    features = proposed.features if re.search(
        r"(?m)^\s*(wifi|bluetooth|deep sleep|jtag|console)\b", statements) else base.features
    return replace(base, pins=tuple(sorted(pins, key=lambda p: p.gpio)), features=features), notes


def try_change(cfg_path: Path, statements: str) -> dict[str, Any]:
    spec = _spec_path(cfg_path)
    base = cnl.parse(spec.read_text("utf-8"))
    changed, notes = apply(base, statements)
    before = {_key(f): f for f in rules.check(base)}
    after = {_key(f): f for f in rules.check(changed)}
    introduced = [after[k].to_dict() for k in after if k not in before]
    resolved = [before[k].to_dict() for k in before if k not in after]
    errors = [f for f in introduced if f["severity"] == "error"]
    return {"ok": not errors, "spec": str(spec), "change": statements.strip(), "applied": notes,
            "introduced": introduced, "resolved": resolved,
            "still_present": len([k for k in after if k in before])}
