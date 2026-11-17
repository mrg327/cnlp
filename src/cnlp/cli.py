"""`cnlp` command line. Thin wiring only: each command calls one module function.

Machine output is JSON on stdout. Exit codes: 0 ok, 1 findings/refusal, 2 usage or input error.

Project layout (directory given by --project, default "."):
  spec.cnl                    controlled English, the source of truth with decisions.jsonl
  .cnlp/manifest.yaml         derived manifest (written by `translate`)
  components/board/           generated ESP-IDF component (written by `gen`)
  decisions.jsonl             append-only gate log
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cnlp.manifest import ManifestError


def _emit(obj: Any) -> None:
    json.dump(obj, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _manifest_path(project: Path) -> Path:
    return project / ".cnlp" / "manifest.yaml"


def cmd_translate(a: argparse.Namespace) -> int:
    from cnlp import cnl

    src = Path(a.file) if a.file else a.project / "spec.cnl"
    text = src.read_text()
    try:
        m = cnl.parse(text)
    except cnl.CnlError as e:
        _emit({"ok": False, "errors": e.to_list()})
        return 1
    out = _manifest_path(a.project)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(m.to_yaml())
    rt = cnl.render(m)
    _emit({"ok": True, "manifest": str(out), "assumptions": cnl.assumptions(m, text=text),
           "round_trip_ok": cnl.parse(rt) == m})
    return 0


def cmd_render(a: argparse.Namespace) -> int:
    from cnlp import cnl, manifest

    sys.stdout.write(cnl.render(manifest.load(_manifest_path(a.project))))
    return 0


def cmd_check_pins(a: argparse.Namespace) -> int:
    from cnlp import manifest, rules

    m = manifest.load(a.manifest or _manifest_path(a.project))
    findings = rules.check(m)
    _emit({"ok": not any(f.severity == "error" for f in findings),
           "findings": [f.to_dict() for f in findings]})
    return 1 if any(f.severity == "error" for f in findings) else 0


def cmd_gen(a: argparse.Namespace) -> int:
    from cnlp import codegen, manifest, rules

    m = manifest.load(_manifest_path(a.project))
    errors = [f for f in rules.check(m) if f.severity == "error"]
    if errors:
        _emit({"ok": False, "reason": "pin rules failed; run check-pins",
               "findings": [f.to_dict() for f in errors]})
        return 1
    files = codegen.generate(m, a.project / "components" / "board")
    _emit({"ok": True, "files": files})
    return 0


def cmd_gate(a: argparse.Namespace) -> int:
    from cnlp import gates

    if a.gate_cmd == "next":
        _emit(gates.next_gate(a.project, only=a.gate))
        return 0
    ok, msg = gates.answer(a.project, a.id, a.hash, a.choice, a.note)
    _emit({"ok": ok, "message": msg})
    return 0 if ok else 1


def cmd_flash(a: argparse.Namespace) -> int:
    from cnlp import broker

    result = broker.flash(a.project, port=a.port)
    _emit(result)
    return 0 if result.get("ok") else 1


def cmd_readback(a: argparse.Namespace) -> int:
    from cnlp import manifest, readback

    m = manifest.load(_manifest_path(a.project))
    diffs = readback.diff(m, readback.parse(Path(a.log).read_text()))
    _emit({"ok": not diffs, "mismatches": diffs})
    return 0 if not diffs else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cnlp", description=__doc__.splitlines()[0])
    p.add_argument("--project", type=Path, default=Path("."))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("translate", help="controlled English -> manifest")
    s.add_argument("file", nargs="?")
    s.set_defaults(fn=cmd_translate)

    sub.add_parser("render", help="manifest -> controlled English").set_defaults(fn=cmd_render)

    s = sub.add_parser("check-pins", help="run chip and module pin rules")
    s.add_argument("--manifest", type=Path)
    s.set_defaults(fn=cmd_check_pins)

    sub.add_parser("gen", help="generate the board component").set_defaults(fn=cmd_gen)

    s = sub.add_parser("gate", help="review and answer approval gates")
    gs = s.add_subparsers(dest="gate_cmd", required=True)
    n = gs.add_parser("next")
    n.add_argument("--gate", dest="gate")
    an = gs.add_parser("answer")
    an.add_argument("id")
    an.add_argument("hash")
    an.add_argument("choice", choices=["approve", "change", "reject"])
    an.add_argument("note", nargs="?", default="")
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("flash", help="flash the approved image, then verify readback")
    s.add_argument("--port", default="/dev/ttyUSB0")
    s.set_defaults(fn=cmd_flash)

    s = sub.add_parser("readback", help="diff a captured serial log against the manifest")
    s.add_argument("log")
    s.set_defaults(fn=cmd_readback)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (ManifestError, FileNotFoundError) as e:
        _emit({"ok": False, "error": str(e)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
