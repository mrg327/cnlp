"""Hand-edit sync: detect edits to generated files and turn the understood ones into spec changes.

`record` stores the hash of every generated file. `scan` compares disk with that record. To
understand an edit it regenerates the pristine files from `.cnlp/manifest.yaml` and diffs the
edited file against them. Only a small set of edits is understood (see `_scan_header` and
`_scan_pins_c`); everything else is reported as unrecognized and is never accepted silently.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import tempfile
from pathlib import Path

from cnlp import cnl, codegen, manifest
from cnlp.manifest import Manifest, Pin

OUT_DIR = "components/board"
HEADER = "include/board_pins.h"
PINS_C = "board_pins.c"
UNRECOGNIZED = ("This edit can't be expressed in the spec yet. Put the change in spec.cnl, "
                "or revert it with `cnlp gen`.")

_DEFINE = re.compile(r"^#define (PIN_[A-Z0-9_]+) GPIO_NUM_(\d+)[ \t]*$", re.MULTILINE)
_CFG = re.compile(r"cfg_(\d+) = \{(.*?)\};", re.DOTALL)
_UP = re.compile(r"^(\s*)\.pull_up_en = (\w+),[ \t]*$", re.MULTILINE)
_DOWN = re.compile(r"^(\s*)\.pull_down_en = (\w+),[ \t]*$", re.MULTILINE)


def _gen_path(project: Path) -> Path:
    return project / ".cnlp" / "generated.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record(project: Path, files: list[dict]) -> None:
    project = Path(project)
    mpath = project / ".cnlp" / "manifest.yaml"
    data = {
        "manifest_sha256": _sha(mpath.read_bytes()),
        "out_dir": OUT_DIR,
        "files": {f"{OUT_DIR}/{f['path']}": f["sha256"] for f in files},
    }
    _gen_path(project).parent.mkdir(parents=True, exist_ok=True)
    _gen_path(project).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _pull_of(up: str, down: str) -> str | None:
    u, d = up.endswith("ENABLE"), down.endswith("ENABLE")
    if u and d:
        return None
    return "internal_up" if u else "internal_down" if d else "none"


def _scan_header(base: str, disk: str, m: Manifest, rel: str, edits: list[dict], unrec: list[dict]):
    """Understood: changed, added or removed `#define PIN_<ROLE> GPIO_NUM_<n>` lines."""
    b = {k: int(v) for k, v in _DEFINE.findall(base)}
    d = {k: int(v) for k, v in _DEFINE.findall(disk)}
    by_name = {"PIN_" + p.role.upper(): p for p in m.pins}
    for name in sorted(set(b) | set(d)):
        if b.get(name) == d.get(name):
            continue
        if name in b and name in d:
            edits.append({"file": rel, "kind": "move_pin", "role": by_name[name].role,
                          "gpio": d[name], "what": f"{name} moved from GPIO {b[name]} to GPIO {d[name]}"})
        elif name in b:
            edits.append({"file": rel, "kind": "remove_pin", "role": by_name[name].role,
                          "what": f"{name} was removed"})
        else:
            edits.append({"file": rel, "kind": "add_pin", "role": name[4:].lower(), "gpio": d[name],
                          "what": f"{name} was added on GPIO {d[name]} "
                                  "(assumed input with no pull; say otherwise in the spec)"})
    if _DEFINE.sub("", base) != _DEFINE.sub("", disk):
        unrec.append({"file": rel, "message": UNRECOGNIZED})


def _cfg_pulls(text: str) -> dict[int, tuple[str, str]]:
    out = {}
    for n, body in _CFG.findall(text):
        u, dn = _UP.search(body), _DOWN.search(body)
        if u and dn:
            out[int(n)] = (u.group(2), dn.group(2))
    return out


def _scan_pins_c(base: str, disk: str, m: Manifest, rel: str, edits: list[dict], unrec: list[dict]):
    """Understood: changed `.pull_up_en` / `.pull_down_en` inside a pin's gpio_config block."""
    b, d = _cfg_pulls(base), _cfg_pulls(disk)
    for gpio in sorted(set(b) | set(d)):
        if b.get(gpio) == d.get(gpio):
            continue
        pins = [p for p in m.pins if p.gpio == gpio and p.function in ("gpio", "interrupt")]
        pull = _pull_of(*d[gpio]) if gpio in d else None
        if not pins or pull is None:
            unrec.append({"file": rel, "message": UNRECOGNIZED})
            continue
        edits.append({"file": rel, "kind": "change_pull", "role": pins[0].role, "pull": pull,
                      "what": f"the pull on GPIO {gpio} ({pins[0].role}) was changed to {pull}"})
    norm = lambda t: _DOWN.sub(r"\1.pull_down_en = X,", _UP.sub(r"\1.pull_up_en = X,", t))
    if norm(base) != norm(disk):
        unrec.append({"file": rel, "message": UNRECOGNIZED})


def _apply_edit(m: Manifest, e: dict) -> Manifest:
    pins = list(m.pins)
    idx = next((i for i, p in enumerate(pins) if p.role == e["role"]), None)
    if e["kind"] == "move_pin":
        pins[idx] = dataclasses.replace(pins[idx], gpio=e["gpio"])
    elif e["kind"] == "change_pull":
        pins[idx] = dataclasses.replace(pins[idx], pull=e["pull"])
    elif e["kind"] == "remove_pin":
        del pins[idx]
    elif e["kind"] == "add_pin":
        pins.append(Pin(gpio=e["gpio"], role=e["role"], mode="input", pull="none"))
    return dataclasses.replace(m, pins=tuple(pins))


def _diff_lines(a: Manifest, b: Manifest) -> tuple[str, str]:
    la, lb = cnl.render(a).splitlines(), cnl.render(b).splitlines()
    return ("\n".join(x for x in la if x not in lb), "\n".join(x for x in lb if x not in la))


def _analyze(project: Path) -> dict:
    project = Path(project)
    gp = _gen_path(project)
    if not gp.exists():
        return {"ok": False, "edited": [], "missing": [], "proposals": [], "unrecognized": [],
                "message": "Run `cnlp gen` first."}
    rec = json.loads(gp.read_text())
    prefix = rec["out_dir"] + "/"
    edited, missing = [], []
    for rel, sha in sorted(rec["files"].items()):
        f = project / rel
        if not f.exists():
            missing.append(rel)
        elif _sha(f.read_bytes()) != sha:
            edited.append(rel)
    res = {"ok": False, "edited": edited, "missing": missing, "proposals": [], "unrecognized": [],
           "_manifest": None, "_modified": None}
    if not edited:
        res["ok"] = not missing
        return res
    m0 = manifest.load(project / ".cnlp" / "manifest.yaml")
    with tempfile.TemporaryDirectory() as tmp:
        regen = codegen.generate(m0, Path(tmp))
        base_sha = {prefix + f["path"]: f["sha256"] for f in regen}
        if base_sha != rec["files"]:
            res["message"] = ("The manifest changed since the last `cnlp gen`, so edits can't be "
                              "compared. Run `cnlp gen` first.")
            return res
        edits: list[dict] = []
        for rel in edited:
            sub = rel[len(prefix):]
            disk = (project / rel).read_text(errors="replace")
            base = (Path(tmp) / sub).read_text()
            if sub == HEADER:
                _scan_header(base, disk, m0, rel, edits, res["unrecognized"])
            elif sub == PINS_C:
                _scan_pins_c(base, disk, m0, rel, edits, res["unrecognized"])
            else:
                res["unrecognized"].append({"file": rel, "message": UNRECOGNIZED})
    cur = m0
    for e in edits:
        after = _apply_edit(m0, e)
        before_line, after_line = _diff_lines(m0, after)
        res["proposals"].append({"file": e["file"], "kind": e["kind"], "before": before_line,
                                 "after": after_line,
                                 "message": f"{e['what']}. Proposed spec change: "
                                            f"{before_line or '(none)'} -> {after_line or '(removed)'}"})
        cur = _apply_edit(cur, e)
    res["_manifest"], res["_modified"] = m0, cur
    return res


def scan(project: Path) -> dict:
    res = _analyze(project)
    res.pop("_manifest", None)
    res.pop("_modified", None)
    return res


def proposed_spec(project: Path) -> str | None:
    """Canonical spec text with every understood edit applied (replaces spec.cnl's formatting)."""
    res = _analyze(project)
    if not res["proposals"]:
        return None
    return cnl.render(res["_modified"])


def apply(project: Path) -> dict:
    project = Path(project)
    res = _analyze(project)
    if res["unrecognized"]:
        return {"ok": False, "message": UNRECOGNIZED + " Files: "
                + ", ".join(u["file"] for u in res["unrecognized"])}
    if not res["proposals"]:
        return {"ok": False, "message": res.get("message", "No understood hand edits to apply.")}
    text = cnl.render(res["_modified"])
    cnl.parse(text)  # must round-trip, else refuse before touching spec.cnl
    spec = project / "spec.cnl"
    bak = project / ".cnlp" / "spec.cnl.bak"
    bak.parent.mkdir(parents=True, exist_ok=True)
    if spec.exists():
        bak.write_text(spec.read_text())
    spec.write_text(text)
    return {"ok": True,
            "message": "spec.cnl was rewritten in canonical form (old copy kept at "
                       ".cnlp/spec.cnl.bak). Run `cnlp translate`, then `cnlp gate review` "
                       "to approve the new spec; `cnlp gen` will then overwrite the hand edits."}
