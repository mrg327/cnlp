"""`cnlp` command line. Thin wiring only: each command calls one module function.

Output is readable text by default; `--json` (before or after the subcommand) prints the raw
JSON instead. Exit codes: 0 ok, 1 findings/refusal, 2 usage or input error.

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


def _formatter(a: argparse.Namespace) -> Any:
    from cnlp import output as o

    name = a.fn.__name__.removeprefix("cmd_")
    if name == "gate":
        name = "gate_" + a.gate_cmd
    if name == "evidence":
        name = "evidence_add" if a.ev_cmd == "add" else "evidence"
    table = {"check_pins": o.fmt_findings("Pins"), "check_fsm": o.fmt_findings("State machines"),
             "lint": o.fmt_findings("Lint")}
    return table.get(name) or getattr(o, "fmt_" + name)


def _out(a: argparse.Namespace, obj: Any) -> None:
    """JSON with --json, else the human formatter (errors go to stderr as text)."""
    if getattr(a, "json", False):
        _emit(obj)
        return
    from cnlp import output

    st = output.Style(project=str(a.project))
    if isinstance(obj, dict) and obj.get("ok") is False and "error" in obj:
        sys.stderr.write(output.fmt_error(obj, output.Style(sys.stderr, project=str(a.project))) + "\n")
        return
    sys.stdout.write(_formatter(a)(obj, st) + "\n")


def cmd_try(a: argparse.Namespace) -> int:
    from cnlp import audit, whatif

    try:
        cfg = audit.find_config(Path(a.project_dir))
        result = whatif.try_change(cfg, a.statements)
    except (audit.AuditError, whatif.WhatIfError) as e:
        _out(a, {"ok": False, "error": str(e)})
        return 2
    _out(a, result)
    return 0 if result["ok"] else 1


def cmd_config(a: argparse.Namespace) -> int:
    """Print the cnlp config that covers a directory (exit 1 if none). Cheap: reads no target files."""
    from cnlp import audit

    try:
        cfg = audit.find_config(Path(a.dir))
    except audit.AuditError as e:
        if getattr(a, "json", False):
            _emit({"ok": False, "error": str(e)})
        else:
            sys.stderr.write(str(e) + "\n")
        return 1
    if getattr(a, "json", False):
        _emit({"ok": True, "config": str(cfg), "name": cfg.parent.name})
    else:
        sys.stdout.write(f"{cfg}\n")
    return 0


def cmd_ask(a: argparse.Namespace) -> int:
    from cnlp import ask as asker
    from cnlp import audit, llm, output

    cfg = None
    try:  # explicit config, else the project in the current directory, else tool-only
        cfg = audit.find_config(Path(a.project_config or "."))
    except audit.AuditError:
        if a.project_config:
            _out(a, {"ok": False, "error": f"No cnlp config for {a.project_config}"})
            return 2
    try:
        res = asker.ask(a.question, cfg, run=a.run)
    except llm.LlmError as e:
        _out(a, {"ok": False, "error": str(e)})
        return 2
    if getattr(a, "json", False):
        _emit(res.to_dict())
    else:
        sys.stdout.write(asker.format_text(res, output.Style(project=str(a.project))) + "\n")
    return 1 if res.kind == "unknown" else 0


def _manifest_path(project: Path) -> Path:
    return project / ".cnlp" / "manifest.yaml"


def cmd_translate(a: argparse.Namespace) -> int:
    from cnlp import cnl

    src = Path(a.file) if a.file else a.project / "spec.cnl"
    text = src.read_text()
    try:
        m = cnl.parse(text)
    except cnl.CnlError as e:
        _out(a, {"ok": False, "errors": e.to_list()})
        return 1
    out = _manifest_path(a.project)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(m.to_yaml())
    rt = cnl.render(m)
    _out(a, {"ok": True, "manifest": str(out), "assumptions": cnl.assumptions(m, text=text),
           "round_trip_ok": cnl.parse(rt) == m})
    return 0


def cmd_spec(a: argparse.Namespace) -> int:
    from cnlp import llm, nl

    request = Path(a.file).read_text() if a.file else a.request
    if not request:
        _out(a, {"ok": False, "error": "Give a request in quotes, or --file request.md"})
        return 2
    try:
        r = nl.translate(request, n=a.n, board=a.board)
    except llm.LlmError as e:
        _out(a, {"ok": False, "error": str(e)})
        return 2
    a.project.mkdir(parents=True, exist_ok=True)
    nl.write_project(a.project, request, r)
    _out(a, {"ok": r.ok, "agreed": r.agreed, "spec": str(a.project / "spec.cnl"),
           "questions": r.questions, "assumptions": r.assumptions,
           "next": "Answer the questions by editing spec.cnl, then run `cnlp translate` "
                   "and `cnlp gate review`."})
    return 0 if r.ok else 1


def cmd_render(a: argparse.Namespace) -> int:
    from cnlp import cnl, manifest

    sys.stdout.write(cnl.render(manifest.load(_manifest_path(a.project))))
    return 0


def cmd_check_pins(a: argparse.Namespace) -> int:
    from cnlp import manifest, rules

    m = manifest.load(a.manifest or _manifest_path(a.project))
    findings = rules.check(m)
    _out(a, {"ok": not any(f.severity == "error" for f in findings),
           "findings": [f.to_dict() for f in findings]})
    return 1 if any(f.severity == "error" for f in findings) else 0


def cmd_gen(a: argparse.Namespace) -> int:
    from cnlp import codegen, fsm, manifest, rules

    m = manifest.load(_manifest_path(a.project))
    errors = [f for f in rules.check(m) + fsm.check(m) if f.severity == "error"]
    if errors:
        _out(a, {"ok": False, "reason": "pin or state-machine rules failed; run check-pins / check-fsm",
               "findings": [f.to_dict() for f in errors]})
        return 1
    from cnlp import sync

    files = codegen.generate(m, a.project / "components" / "board")
    sync.record(a.project, files)
    _out(a, {"ok": True, "files": files})
    return 0


def cmd_check_fsm(a: argparse.Namespace) -> int:
    from cnlp import fsm, manifest

    m = manifest.load(_manifest_path(a.project))
    findings = fsm.check(m)
    errors = any(f.severity == "error" for f in findings)
    _out(a, {"ok": not errors, "findings": [f.to_dict() for f in findings]})
    return 1 if errors else 0


def cmd_audit(a: argparse.Namespace) -> int:
    from cnlp import audit

    try:
        cfg = audit.find_config(Path(a.config))
    except audit.AuditError as e:
        _out(a, {"ok": False, "error": str(e)})
        return 2
    result = audit.audit(cfg)
    if a.save:
        audit.write_report(result, cfg)
    _out(a, result)
    return 0 if result.get("ok") else 1


def cmd_lint(a: argparse.Namespace) -> int:
    from cnlp import lint

    findings = lint.run(a.project)
    errors = any(f.severity == "error" for f in findings)
    _out(a, {"ok": not errors, "findings": [f.to_dict() for f in findings]})
    return 1 if errors else 0


def cmd_sync(a: argparse.Namespace) -> int:
    from cnlp import sync

    if a.apply:
        result = sync.apply(a.project)
    else:
        result = sync.scan(a.project)
        spec = sync.proposed_spec(a.project) if result.get("proposals") else None
        if spec:
            result["proposed_spec"] = spec
    _out(a, result)
    return 0 if result.get("ok") else 1


def cmd_gate(a: argparse.Namespace) -> int:
    from cnlp import gates

    if a.gate_cmd == "review":
        if not sys.stdin.isatty():
            _out(a, {"ok": False, "error": "gate review is interactive; use gate next / gate answer"})
            return 2
        return gates.review(a.project)
    if a.gate_cmd == "next":
        _out(a, gates.next_gate(a.project, only=a.gate))
        return 0
    ok, msg = gates.answer(a.project, a.id, a.hash, a.choice, a.note)
    _out(a, {"ok": ok, "message": msg})
    return 0 if ok else 1


def cmd_flash(a: argparse.Namespace) -> int:
    from cnlp import broker

    result = broker.flash(a.project, port=a.port)
    _out(a, result)
    return 0 if result.get("ok") else 1


def cmd_readback(a: argparse.Namespace) -> int:
    from cnlp import manifest, readback

    m = manifest.load(_manifest_path(a.project))
    diffs = readback.diff(m, readback.parse(Path(a.log).read_text()))
    _out(a, {"ok": not diffs, "mismatches": diffs})
    return 0 if not diffs else 1


def cmd_measure(a: argparse.Namespace) -> int:
    from cnlp import audit, evidence, measure

    try:
        if a.raw_query is None and not a.kind:
            raise measure.MeasureError("give a measurement kind ("
                                       + ", ".join(measure.KINDS) + ") or --raw-query")
        if a.record and a.raw_query is not None:
            raise measure.MeasureError("--record can't be combined with --raw-query")
        if a.record and not a.criterion:
            raise measure.MeasureError("--record requires --criterion")
        if a.criterion:
            measure.parse_criterion(a.criterion)  # fail before touching the instrument
        cfg = None
        if a.record:
            cfg = audit.find_config(Path(a.record[0]))
        sc = measure.connect(a.resource)
        try:
            if a.raw_query is not None:
                _out(a, measure.raw_query(sc, a.raw_query))
                return 0
            res = measure.measure(sc, a.kind, a.ch, a.ch2, a.samples, a.interval)
        finally:
            sc.close()
        rc = 0
        if a.criterion:
            ok, why = measure.check(res, a.criterion)
            res["check"] = {"criterion": a.criterion, "ok": ok, "explanation": why}
            rc = 0 if ok else 1
            if cfg is not None:
                rec = measure.record(cfg, a.record[1], res, a.criterion, ok, a.source_note or "")
                res["recorded"] = {"req": a.record[1], "type": "measured_scope",
                                   "result": "pass" if ok else "fail", **rec}
        _out(a, res)
        return rc
    except (measure.MeasureError, audit.AuditError, evidence.EvidenceError) as e:
        _out(a, {"ok": False, "error": str(e)})
        return 2


def cmd_evidence(a: argparse.Namespace) -> int:
    from cnlp import audit

    try:
        from cnlp import evidence
    except ImportError as e:
        _out(a, {"ok": False, "error": f"evidence support is not available yet ({e})"})
        return 2
    args = list(a.args)
    a.ev_cmd = "add" if args[0] == "add" and len(args) > 1 else "show"
    if a.ev_cmd == "add":
        args = args[1:]
        if len(args) != 4 or args[3] not in ("pass", "fail"):
            _out(a, {"ok": False, "error": "usage: cnlp evidence add <config> <REQ> <type> pass|fail"
                                           " [--value V --unit U --criterion C --source S --note N]"})
            return 2
    elif len(args) != 1:
        _out(a, {"ok": False, "error": "usage: cnlp evidence <config>"})
        return 2
    try:
        cfg = audit.find_config(Path(args[0]))
        if a.ev_cmd == "add":
            rec = evidence.add(cfg, args[1], args[2], args[3], value=a.value, unit=a.unit,
                               criterion=a.criterion, source=a.source, note=a.note)
            if isinstance(rec, dict):
                rec = {"req": args[1], "type": args[2], "result": args[3], "value": a.value,
                       "unit": a.unit, "criterion": a.criterion, **rec}
            _out(a, rec)
            return 0
        result = evidence.build(cfg)
    except (audit.AuditError, ValueError, KeyError) as e:
        _out(a, {"ok": False, "error": str(e)})
        return 2
    _out(a, result)
    return 0 if result.get("ok") else 1


def cmd_status(a: argparse.Namespace) -> int:
    from cnlp import audit, status

    try:
        result = status.status(audit.find_config(Path(a.config)))
    except (audit.AuditError, ValueError, KeyError, OSError) as e:
        _out(a, {"ok": False, "error": str(e)})
        return 2
    _out(a, result)
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cnlp", description=__doc__.splitlines()[0])
    p.add_argument("--project", type=Path, default=Path("."))
    p.add_argument("--json", action="store_true", help="print raw JSON instead of readable text")
    # Same flag after the subcommand; SUPPRESS keeps a value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print raw JSON instead of readable text")
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *x, **k: _add(*x, parents=[common], **k)  # type: ignore[method-assign]

    s = sub.add_parser("translate", help="controlled English -> manifest")
    s.add_argument("file", nargs="?")
    s.set_defaults(fn=cmd_translate)

    s = sub.add_parser("spec", help="plain English -> spec.cnl via the local model")
    s.add_argument("request", nargs="?")
    s.add_argument("--file")
    s.add_argument("-n", type=int, default=3, help="samples for self-consistency")
    s.add_argument("--board", default="devkit-v1")
    s.set_defaults(fn=cmd_spec)

    s = sub.add_parser("try", help="what-if: check a proposed pin change against all pin rules")
    s.add_argument("statements", help='controlled English, e.g. \'gpio 35 is input "enc sw" with internal pull-up.\'')
    s.add_argument("project_dir", nargs="?", default=".")
    s.set_defaults(fn=cmd_try)

    s = sub.add_parser("config", help="show which cnlp config covers a directory")
    s.add_argument("dir", nargs="?", default=".")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("ask", help="ask a question about cnlp, your project or the board")
    s.add_argument("question")
    s.add_argument("--project-config", help="project dir or cnlp config (default: current dir)")
    s.add_argument("--run", action="store_true", help="run the suggested command if it is read-only")
    s.set_defaults(fn=cmd_ask)

    sub.add_parser("render", help="manifest -> controlled English").set_defaults(fn=cmd_render)

    s = sub.add_parser("check-pins", help="run chip and module pin rules")
    s.add_argument("--manifest", type=Path)
    s.set_defaults(fn=cmd_check_pins)

    sub.add_parser("check-fsm", help="check state machines").set_defaults(fn=cmd_check_fsm)

    sub.add_parser("gen", help="generate the board component").set_defaults(fn=cmd_gen)

    sub.add_parser("lint", help="lint app code for pin, include and pure-core rules"
                   ).set_defaults(fn=cmd_lint)

    s = sub.add_parser("audit", help="read-only audit of an existing project (works on a snapshot)")
    s.add_argument("config", help="the project dir to audit, projects/<name>, or an audit.yaml")
    s.add_argument("--save", action="store_true", help="also write <name>/.cnlp/audit.json")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("sync", help="detect hand edits to generated files")
    s.add_argument("--apply", action="store_true", help="write the proposed spec.cnl")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("gate", help="review and answer approval gates")
    gs = s.add_subparsers(dest="gate_cmd", required=True)
    _gadd = gs.add_parser
    gs.add_parser = lambda *x, **k: _gadd(*x, parents=[common], **k)  # type: ignore[method-assign]
    gs.add_parser("review", help="interactively review each pending gate in order")
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

    s = sub.add_parser("measure", help="read the bench scope over SCPI; optionally record evidence")
    s.add_argument("kind", nargs="?", choices=["period", "frequency", "pwidth", "nwidth",
                                               "duty", "delay"])
    s.add_argument("--ch", type=int, default=1)
    s.add_argument("--ch2", type=int, help="second channel for delay (default 2)")
    s.add_argument("--samples", type=int, default=20)
    s.add_argument("--interval", type=float, default=0.2, help="seconds between samples")
    s.add_argument("--resource", help="VISA resource (default: SCOPE_RESOURCE or LAN search)")
    s.add_argument("--criterion", help='e.g. "< 1.6 ms", "1 ms ±2%%", "max < 2.67 ms"')
    s.add_argument("--record", nargs=2, metavar=("PROJECT", "REQ"),
                   help="record as measured_scope evidence (needs --criterion)")
    s.add_argument("--source-note", dest="source_note", help="note stored with the evidence")
    s.add_argument("--raw-query", dest="raw_query", help="send one SCPI query and print the reply")
    s.set_defaults(fn=cmd_measure)

    s = sub.add_parser("evidence", help="requirements evidence matrix; `evidence add` records one")
    s.add_argument("args", nargs="+", metavar="config | add config REQ type pass|fail")
    s.add_argument("--value")
    s.add_argument("--unit")
    s.add_argument("--criterion")
    s.add_argument("--source")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_evidence)

    s = sub.add_parser("status", help="build phase plus audit/real-time/evidence summary")
    s.add_argument("config", help="audit.yaml, its folder, or the target project directory")
    s.set_defaults(fn=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (ManifestError, FileNotFoundError) as e:
        _out(a, {"ok": False, "error": str(e)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
