"""Knowledge/retrieval layer for `cnlp ask`.

`build_index()` turns cnlp's own docs, CLI, rules and (optionally) a project's plan, audit
and evidence into short citeable `Chunk`s; `Index.retrieve()` ranks them with BM25 (stdlib
only). Anything read from a target project goes through the existing snapshotting
functions (`audit.audit`, `evidence.build`, `phases.load`); nothing is read in place here.
Config-side files (spec.cnl, requirements.yaml) live in the cnlp repo and are read directly.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

MAX_TEXT = 1200
_REPO = Path(__file__).resolve().parents[2]

# Commands that never change anything outside a temp snapshot.
_READ_ONLY = {"render", "check-pins", "check-fsm", "lint", "audit", "evidence", "status",
              "gate next", "sync", "try", "config"}

# Plain-language "use this when" text, so questions phrased without the command name still land.
_BLURB = {
    "translate": "Parse spec.cnl controlled English into the pin manifest (writes .cnlp).",
    "spec": "Turn a plain-English request into spec.cnl using the local model; writes files.",
    "render": "Print the manifest back as controlled English (read the pin plan).",
    "check-pins": "Run the chip and module pin rules on the manifest: which pins are unsafe, "
                  "strapping, input-only.",
    "check-fsm": "Check the state machines for unreachable states and missing transitions.",
    "gen": "Generate the board component C code from the manifest (writes files).",
    "lint": "Lint application C code for direct GPIO use, banned includes and real-time rules.",
    "audit": "Check my code against the pin plan: read-only audit of an existing project, finds "
             "pins the code uses that the plan lacks, wrong mode or pull, conflicts, and lint.",
    "sync": "Detect hand edits to generated files; --apply writes a proposed spec.cnl.",
    "gate": "Approval gates: review pending approvals, answer approve, change or reject.",
    "gate review": "Interactively review each pending approval gate (writes decisions).",
    "gate next": "Show the next pending approval gate (read-only).",
    "gate answer": "Record an approve, change or reject decision for a gate (writes decision log).",
    "flash": "Flash the approved firmware image to the board, then verify register readback.",
    "readback": "Diff a captured serial log against the manifest (what the chip really configured).",
    "measure": "Read the bench oscilloscope over SCPI (period, frequency, pulse width, delay); "
               "optionally records the result as evidence. Needs the scope on the network.",
    "evidence": "Requirements evidence matrix: which requirement is pass, fail, missing or stale. "
                "`evidence add` records a measurement, scope reading, test or review result "
                "as evidence for a requirement.",
    "evidence add": "Record a measurement, scope result, test or review result as evidence for "
                    "a requirement (appends to evidence.jsonl).",
    "status": "Where the project is in its build procedure (phase) plus audit, real-time and "
              "evidence summary: what to do next, and the phase watch-list of common problems.",
    "try": "What-if for a pin change: can I move a pin, use GPIO N for something, would this "
           "break a rule? Checks one proposed pin line against all pin rules without changing "
           "anything. Pin-line format: gpio <N> is <input|output|adc|...> \"<role>\" with "
           "<no pull|internal pull-up|internal pull-down|external pull-up|off-board pull-up>. "
           "Example: cnlp try 'gpio 5 is output \"probe scan\" with no pull.' A pin line whose "
           "role already exists on another GPIO is a move of that role.",
    "config": "Show which cnlp config (projects/<name>/audit.yaml) covers a directory.",
}

_AUDIT_RULES = {
    "AUDIT_UNPLANNED": "Code configures a GPIO that spec.cnl does not plan.",
    "AUDIT_UNUSED": "Pin is in the plan but the code does not configure it yet (\"not configured "
                    "in code yet\"); expected while a project is mid-build.",
    "AUDIT_MODE": "Code configures the pin with a different mode (input/output) than planned.",
    "AUDIT_PULL": "Code enables a different pull (internal/none) than planned.",
    "AUDIT_DRIVER": "Code drives the pin through a different peripheral or driver than planned.",
    "AUDIT_CONFLICT": "Two uses in the code are incompatible on the same pin.",
    "AUDIT_UNRESOLVED": "A pin number in the code could not be resolved statically.",
}
_LINT_RULES = {
    "LINT_DIRECT_GPIO": "Application code calls gpio_* directly instead of the generated board "
                        "component.",
    "LINT_DRIVER_PIN_FIELD": "A driver config struct sets a pin field by raw number instead of "
                             "a PIN_* name.",
    "LINT_INCLUDE": "Include is neither on the allow list nor a project header.",
    "LINT_BANNED_API": "Header on the profile's include_ban list (the message names the "
                       "replacement).",
    "LINT_PURE_CORE": "ESP-IDF or FreeRTOS include under a `core` path: pure core code must stay "
                      "hardware-free.",
    "LINT_PARSE": "Warning: tree-sitter could not fully parse the file; it is still scanned.",
}
# chip rule id -> pin_sets key listing the pins it applies to
_RULE_SET = {"C1": "not_bonded", "C2": "input_only", "C3": "flash", "C4": "psram",
             "C10": "adc2", "C11": "adc_interrupt_errata", "C12": "uart0", "C13": "jtag",
             "C14": "rtc", "C15": "dac"}


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str
    title: str
    text: str
    kind: str  # doc | command | rule | pin | requirement | status | spec


def _clip(text: str, n: int = MAX_TEXT) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "section"


def _pieces(text: str, n: int = MAX_TEXT) -> list[str]:
    """Split text into <= n char pieces on paragraph, then line boundaries."""
    out: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", text.strip()):
        for part in _split_long(para, n):
            if cur and len(cur) + len(part) + 2 > n:
                out.append(cur)
                cur = part
            else:
                cur = f"{cur}\n\n{part}" if cur else part
    if cur:
        out.append(cur)
    return out


def _split_long(para: str, n: int) -> list[str]:
    if len(para) <= n:
        return [para]
    out, cur = [], ""
    for line in para.split("\n"):
        while len(line) > n:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:n])
            line = line[n:]
        if cur and len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- commands

def _arg_info(a: Any) -> dict:
    flags = list(a.option_strings)
    d = {"name": ", ".join(flags) if flags else a.dest, "dest": a.dest,
         "positional": not flags, "help": a.help or "", "required": bool(
             a.required if flags else a.nargs not in ("?", "*")),
         "choices": list(a.choices) if a.choices else None}
    if a.default not in (None, False, ".") and a.default is not __import__("argparse").SUPPRESS:
        d["default"] = str(a.default)
    return d


def _usage(parser: Any, prog: str) -> str:
    parser.prog = prog
    return " ".join(parser.format_usage().replace("usage:", "", 1).split())


def commands() -> list[dict]:
    """[{name, help, usage, read_only, args}] for every CLI command (gate has sub-entries)."""
    import argparse

    from cnlp.cli import build_parser

    root = build_parser()
    sub = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    helps = {ca.dest: (ca.help or "") for ca in sub._choices_actions}
    out: list[dict] = []

    def args_of(p: Any) -> list[dict]:
        return [_arg_info(a) for a in p._actions
                if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction))
                and a.dest not in ("json",)]

    for name, p in sub.choices.items():
        entry = {"name": name, "help": helps.get(name, ""), "usage": _usage(p, f"cnlp {name}"),
                 "read_only": name in _READ_ONLY, "args": args_of(p)}
        out.append(entry)
        gs = next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)
        if gs:
            gh = {ca.dest: (ca.help or "") for ca in gs._choices_actions}
            for gname, gp in gs.choices.items():
                full = f"{name} {gname}"
                out.append({"name": full, "help": gh.get(gname, ""),
                            "usage": _usage(gp, f"cnlp {full}"),
                            "read_only": full in _READ_ONLY, "args": args_of(gp)})
        if name == "evidence":
            out.append({"name": "evidence add", "help": "record one piece of evidence "
                        "(writes evidence.jsonl in the project config folder)",
                        "usage": "cnlp evidence add CONFIG REQ TYPE pass|fail [--value V] "
                                 "[--unit U] [--criterion C] [--source S] [--note N]",
                        "read_only": False, "args": entry["args"]})
    return out


def _command_chunks() -> list[Chunk]:
    out = []
    for c in commands():
        name = c["name"]
        parts = [f"cnlp {name}: {c['help'] or _BLURB.get(name, '')}".rstrip(": "),
                 _BLURB.get(name, ""),
                 f"Usage: {c['usage']}",
                 "Read-only: yes (never changes the project or the target)." if c["read_only"]
                 else "Read-only: no (writes files, calls a model, or touches hardware)."]
        if name == "sync":
            parts.append("Plain `sync` only scans; `sync --apply` writes spec.cnl.")
        if name == "evidence add":
            parts.append("Records one piece of evidence (measured_scope, tested, measured_serial, "
                         "static, reviewed) for a requirement and writes evidence.jsonl. cnlp "
                         "never records evidence on its own; the user runs this after measuring.")
        if name == "evidence":
            parts.append("Building the matrix is read-only; `evidence add CONFIG REQ TYPE pass|fail "
                         "[--value V --unit U --criterion C --source S --note N]` records "
                         "evidence (measured_scope, tested, measured_serial, static, reviewed) "
                         "and writes evidence.jsonl, so it is not read-only.")
        for a in c["args"]:
            parts.append(f"  {a['name']}: {a['help']}".rstrip(": ")
                         + (f" (choices: {', '.join(a['choices'])})" if a["choices"] else ""))
        cid = "cmd:" + name.replace(" ", "-")
        out.append(Chunk(cid, f"cnlp {name} --help", f"cnlp {name}", _clip("\n".join(
            p for p in parts if p)), "command"))
    return out


# ---------------------------------------------------------------- docs

def _doc_chunks(notes: list[str]) -> list[Chunk]:
    out: list[Chunk] = []
    files = [("readme", "README.md"), ("design", "docs/design.md"), ("plan", "docs/PLAN.md")]
    for key, rel in files:
        path = _REPO / rel
        if not path.is_file():
            notes.append(f"doc {rel} not found; not indexed")
            continue
        sections: list[tuple[str, list[str]]] = [("Overview", [])]
        fence = False
        for line in path.read_text("utf-8").splitlines():
            if line.startswith("```"):
                fence = not fence
            m = None if fence else re.match(r"^(#{1,3})\s+(.+?)\s*$", line)
            if m:
                sections.append((m.group(2), []))
            else:
                sections[-1][1].append(line)
        seen: Counter[str] = Counter()
        for title, lines in sections:
            body = "\n".join(lines).strip()
            if not body:
                continue
            slug = _slug(title)
            seen[slug] += 1
            if seen[slug] > 1:
                slug = f"{slug}-{seen[slug]}"
            pieces = _pieces(body)
            for i, piece in enumerate(pieces):
                cid = f"doc:{key}#{slug}" + (f"-p{i + 1}" if i else "")
                out.append(Chunk(cid, f"{rel} § {title}", title, piece, "doc"))
    return out


# Modules whose docstring explains a feature in plain terms; indexed as `doc:mod-<name>`.
_MODULE_DOCS = ("params", "status", "whatif", "audit", "phases", "measure", "evidence")


def _module_doc_chunks(notes: list[str]) -> list[Chunk]:
    import importlib

    out: list[Chunk] = []
    for name in _MODULE_DOCS:
        try:
            doc = importlib.import_module(f"cnlp.{name}").__doc__ or ""
        except Exception as e:  # noqa: BLE001
            notes.append(f"module cnlp.{name} not indexed: {e}")
            continue
        pieces = _pieces(doc) if doc.strip() else []
        for i, piece in enumerate(pieces[:3]):
            out.append(Chunk(f"doc:mod-{name}" + (f"-p{i + 1}" if i else ""),
                             f"cnlp/{name}.py docstring", f"cnlp {name} module", piece, "doc"))
    return out


# ---------------------------------------------------------------- rules

def _ranges(nums: list[int]) -> str:
    return ", ".join(str(n) for n in sorted(nums))


def _rule_chunks(notes: list[str]) -> list[Chunk]:
    from cnlp.rules import _load

    out: list[Chunk] = []
    for layer, name, label in (("chip", "esp32", "chip rules (esp32.yaml)"),
                               ("module", "esp32-wroom-32", "module rules (esp32-wroom-32.yaml)")):
        try:
            data = _load(layer, name)
        except Exception as e:  # noqa: BLE001
            notes.append(f"{label} unavailable: {e}")
            continue
        sets = data.get("pin_sets", {})
        grouped: dict[str, list[dict]] = {}
        for key, r in data["rules"].items():
            grouped.setdefault(r.get("id", key), []).append(r)
        for rid, rs in grouped.items():
            msgs, seen = [], set()
            for r in rs:
                m = r["message"]
                m = m.replace("GPIO {gpio} ({role}): ", "").replace("{gpio}", "N")
                if m not in seen:
                    seen.add(m)
                    msgs.append(f"[{r['severity']}] {m}")
            pins = ""
            sk = _RULE_SET.get(rid) if layer == "chip" else None
            if sk and sk in sets:
                pins = f"Applies to GPIO {_ranges(sets[sk])}."
            if layer == "chip" and rid in ("C5", "C6", "C7", "C9"):
                pins = f"Applies to GPIO {_ranges(list(sets['strapping_warn']))} (strapping)."
            if layer == "module" and rid == "W1":
                pins = f"Applies to GPIO {_ranges(sets['flash'])}."
            if layer == "module" and rid == "W3":
                pins = "Applies to GPIO 12 (MTDI, sets flash voltage at reset)."
            if layer == "module" and rid == "W4":
                pins = f"Applies to GPIO {_ranges(sets['strapping'])}."
            if layer == "module" and rid == "W5":
                pins = f"Applies to GPIO {_ranges(sets['not_exposed'])}."
            kind = "cross-pin " if rid in ("OWNER", "OPEN_DRAIN_PULL", "I2C_WEAK_PULL",
                                           "FLOATING") else ""
            title = f"{kind}{layer} rule {rid}"
            out.append(Chunk(f"rule:{rid}", label, title,
                             _clip(f"{title}. " + " ".join(msgs) + (f" {pins}" if pins else "")),
                             "rule"))
    have = {c.id for c in out}
    out += _rt_chunks(notes)
    have |= {c.id for c in out}
    out += _emitted_rule_chunks(have, notes)
    return out


# Rule IDs that code emits as string literals; matched the same way by the completeness test.
EMITTED_ID_RX = re.compile(
    r"^(?:(?:AUDIT|LINT|FSM|PARAM|STATUS|READBACK|GATE|SYNC|EVIDENCE)_[A-Z0-9_]+"
    r"|RT\d+_[A-Z0-9_]+|[CW]\d+|OWNER|FLOATING|OPEN_DRAIN_PULL|I2C_WEAK_PULL)$")
_FAMILY = {
    "AUDIT": ("cnlp audit rules", "audit rule", "Reported by `cnlp audit` (code vs pin plan)."),
    "LINT": ("cnlp lint rules (lint.py)", "lint rule", "Reported by `cnlp lint` and `cnlp audit`."),
    "FSM": ("state machine rules (fsm.py)", "state machine rule", "Reported by `cnlp check-fsm`."),
    "PARAM": ("key-parameter audit (params.py)", "key parameter rule",
              ("Reported by `cnlp audit` when the project has a parameters.yaml: expected values "
               "(sdkconfig, constants, driver fields, task priority and core) versus the code.")),
    "STATUS": ("cnlp status (status.py)", "status rule", "Reported by `cnlp status`."),
    "RT": ("real-time lint (rtlint.py)", "real-time rule", "Reported by `cnlp lint`."),
}
_SRC_SKIP = {"knowledge.py", "ask.py", "__init__.py"}


def _msg_text(node: Any) -> str:
    """Readable text of a message expression: literal pieces kept, f-string fields as <...>."""
    import ast

    parts: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            parts.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            for v in n.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                else:
                    parts.append("<...>")
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            walk(n.left)
            walk(n.right)
    walk(node)
    return " ".join("".join(parts).split())


def _scan_emitted() -> dict[str, dict]:
    """{rule id: {file, severity, message}} for rule IDs emitted by calls/dicts in src/cnlp."""
    import ast

    found: dict[str, dict] = {}

    def note(rid: str, file: str, sev: str | None, msg: str) -> None:
        d = found.setdefault(rid, {"file": file, "severity": None, "message": "", "docs": []})
        d["severity"] = d["severity"] or sev
        if len(msg) > len(d["message"]):
            d["message"] = msg

    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        if path.name in _SRC_SKIP:
            continue
        try:
            tree = ast.parse(path.read_text("utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for i, a in enumerate(node.args):
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                            and EMITTED_ID_RX.match(a.value):
                        rest = node.args[i + 1:]
                        sev = next((r.value for r in rest if isinstance(r, ast.Constant)
                                    and r.value in ("error", "warning", "info")), None)
                        msg = next((_msg_text(r) for r in rest
                                    if isinstance(r, (ast.JoinedStr, ast.BinOp))
                                    or (isinstance(r, ast.Constant) and isinstance(r.value, str)
                                        and r.value not in ("error", "warning", "info"))), "")
                        note(a.value, path.name, sev, msg)
            elif isinstance(node, ast.Dict):
                kv = {k.value: v for k, v in zip(node.keys, node.values)
                      if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                r = kv.get("rule")
                if isinstance(r, ast.Constant) and isinstance(r.value, str) \
                        and EMITTED_ID_RX.match(r.value):
                    sv = kv.get("severity")
                    note(r.value, path.name, sv.value if isinstance(sv, ast.Constant) else None,
                         _msg_text(kv["message"]) if "message" in kv else "")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and EMITTED_ID_RX.match(node.value):
                note(node.value, path.name, None, "")
        doc = ast.get_docstring(tree) or ""
        for rid in list(found):
            if found[rid]["file"] == path.name or rid in doc:
                for m in re.finditer(rf"\b{re.escape(rid)}\b", doc):
                    ctx = " ".join(doc[max(0, m.start() - 60): m.end() + 260].split())
                    found[rid]["docs"].append((path.name, ctx))
                    break
    return found


def _emitted_rule_chunks(have: set[str], notes: list[str]) -> list[Chunk]:
    """Chunks for every rule ID some module emits that no YAML/docstring chunk covers yet."""
    out: list[Chunk] = []
    blurbs = {**_AUDIT_RULES, **_LINT_RULES}
    for rid, d in sorted(_scan_emitted().items()):
        fam = rid.split("_")[0]
        fam = "RT" if re.match(r"RT\d", fam) else fam
        cid = f"rt:{rid}" if fam == "RT" else f"rule:{rid}"
        if cid in have or (rid[0] in "CW" and rid[1:].isdigit()):
            continue
        label, noun, where = _FAMILY.get(fam, (f"{d['file']}", "rule", f"Emitted by {d['file']}."))
        sev = d["severity"]
        parts = [f"{noun} {rid}." + (f" Severity {sev}." if sev else ""), where]
        if rid in blurbs:
            parts.append(blurbs[rid])
        if d["docs"]:
            parts.append("From the module docs: " + d["docs"][0][1])
        if d["message"]:
            parts.append("Example message: " + d["message"])
        out.append(Chunk(cid, label, f"{noun} {rid}", _clip(" ".join(parts)), "rule"))
    return out
def _rt_chunks(notes: list[str]) -> list[Chunk]:
    from cnlp import rtlint

    doc = rtlint.__doc__ or ""
    out: list[Chunk] = []
    for m in re.finditer(r"^\s{2}(RT\d_[A-Z_0-9]+)\s+\((\w+)\)\s+(.+?)(?=\n\s{2}RT\d_|\n\n)",
                         doc, re.MULTILINE | re.DOTALL):
        rid, sev, d = m.groups()
        d = " ".join(d.split())
        out.append(Chunk(f"rt:{rid}", "real-time lint (rtlint.py)", f"real-time rule {rid}",
                         _clip(f"real-time lint rule {rid} ({sev}, advisory): {d}"), "rule"))
    if not out:
        notes.append("rtlint docstring has no recognisable RT rules")
    lim = doc.split("Limits", 1)
    if len(lim) == 2:
        out.append(Chunk("rt:limits", "real-time lint (rtlint.py)",
                         "real-time lint limits",
                         _clip("Real-time lint limits" + " ".join(lim[1].split())), "rule"))
    return out


# ---------------------------------------------------------------- project

def _hw_notes(gpio: int) -> list[str]:
    from cnlp.rules import _load

    try:
        sets = _load("chip", "esp32")["pin_sets"]
    except Exception:  # noqa: BLE001
        return []
    out = []
    if gpio in sets["input_only"]:
        out.append("Hardware: input-only pin with no output driver and no internal pull-up or "
                   "pull-down (rule C2).")
    if gpio in sets["strapping_warn"] or gpio == 12:
        rid = sets["strapping_warn"].get(gpio, "C8")
        out.append(f"Hardware: strapping pin (rule {rid}); its level at reset sets the boot mode.")
    if gpio in sets["flash"]:
        out.append("Hardware: wired to the SPI flash (rule C3).")
    if gpio in sets["adc2"]:
        out.append("Hardware: ADC2 pin, unusable as analog input while Wi-Fi is on (rule C10).")
    if gpio in sets["dac"]:
        out.append("Hardware: has a DAC output (rule C15).")
    return out


def _spec_chunks(spec_path: Path, proj: str, notes: list[str]) -> list[Chunk]:
    from cnlp import cnl, rules

    if not spec_path.is_file():
        notes.append(f"spec {spec_path.name} not found")
        return []
    text = spec_path.read_text("utf-8")
    out: list[Chunk] = []
    pin_line: dict[int, int] = {}
    stmt_lines: list[tuple[int, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"gpio\s+(\d+)\b", s, re.IGNORECASE)
        if m:
            pin_line[int(m.group(1))] = n
        else:
            stmt_lines.append((n, s))
    try:
        man = cnl.parse(text)
    except Exception as e:  # noqa: BLE001  (CnlError and friends)
        notes.append(f"{spec_path.name} did not parse: {e}")
        man = None
    if man is not None:
        rendered = {int(m.group(1)): m.group(0) for line in cnl.render(man).splitlines()
                    if (m := re.match(r"gpio\s+(\d+)\b.*", line))}
        try:
            findings = rules.check(man)
        except Exception as e:  # noqa: BLE001
            findings = []
            notes.append(f"pin rules failed on {spec_path.name}: {e}")
        for p in man.pins:
            fs = [f for f in findings if f.gpio == p.gpio]
            parts = [f"GPIO {p.gpio} plan: {rendered.get(p.gpio, '')}",
                     f"Role {p.role}, mode {p.mode}, pull {p.pull}, function {p.function}."]
            if p.gpio in pin_line:
                parts.append(f"spec.cnl line {pin_line[p.gpio]}.")
            parts += _hw_notes(p.gpio)
            parts += [f"Rule check {f.rule} ({f.severity}): {f.message}" for f in fs]
            if not fs:
                parts.append("Pin rule check: no findings.")
            out.append(Chunk(f"pin:{proj}:gpio{p.gpio}", f"{proj} pin plan (spec.cnl)",
                             f"GPIO {p.gpio} {p.role}", _clip(" ".join(parts)), "pin"))
    for n, s in stmt_lines:
        out.append(Chunk(f"spec:{proj}:line{n}", f"{proj} spec.cnl line {n}",
                         f"spec.cnl line {n}", _clip(s), "spec"))
    return out


def _req_meta_chunks(base: Path, proj: str) -> list[Chunk]:
    p = base / "requirements.yaml"
    if not p.is_file():
        return []
    meta = (yaml.safe_load(p.read_text("utf-8")) or {}).get("requirements") or {}
    return [Chunk(f"req:{proj}:{rid}", f"{proj} requirements.yaml", f"requirement {rid}",
                  _clip(f"Requirement {rid}: kind {m.get('kind', '?')}; accepted evidence types "
                        f"{', '.join(m.get('accepts') or [])}; static checks "
                        f"{', '.join(m.get('static_checks') or []) or 'none'}."),
                  "requirement") for rid, m in meta.items()]


def _param_chunks(base: Path, cfg: dict, proj: str, notes: list[str]) -> list[Chunk]:
    """Expected key parameters (parameters.yaml in the config folder), one chunk per group."""
    name = cfg.get("parameters")
    if not name:
        return []
    p = base / str(name)
    try:
        data = yaml.safe_load(p.read_text("utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        notes.append(f"parameters {name} unavailable: {e}")
        return []
    src = f"{proj} expected parameters ({name}); check with `cnlp audit`"
    out: list[Chunk] = []
    for group, label in (("tasks", "task priority and core"), ("sdkconfig", "sdkconfig values"),
                         ("constants", "constants (#defines)"),
                         ("driver_fields", "driver config fields"),
                         ("derived", "derived values")):
        g = data.get(group)
        if not isinstance(g, dict) or not g:
            continue
        lines = []
        for k, v in g.items():
            if isinstance(v, dict):
                v = ", ".join(f"{a} {b}" for a, b in v.items())
            lines.append(f"{k}: {v}")
        out.append(Chunk(f"param:{proj}:{group}", src, f"expected {label}", _clip(
            f"Expected key parameters, {label} (from {data.get('source', name)}); "
            "`cnlp audit` flags a differing value with a PARAM_* rule. " + "; ".join(lines) + "."),
            "parameter"))
    return out


def _live_chunks(cfg_path: Path, proj: str, notes: list[str]) -> list[Chunk]:
    from cnlp import audit as audit_mod
    from cnlp import evidence as evidence_mod

    out: list[Chunk] = []
    cfg = yaml.safe_load(cfg_path.read_text("utf-8")) or {}
    try:
        res = audit_mod.audit(cfg_path)
    except Exception as e:  # noqa: BLE001
        notes.append(f"live audit unavailable: {e}")
        res = None
    if res:
        snap = str(res.get("snapshot", {}).get("sha256", ""))[:8]
        src = f"{proj} audit (snapshot {snap})"
        s = res["summary"]
        out.append(Chunk(f"status:{proj}:audit", src, f"{proj} audit summary", _clip(
            f"Audit of {proj}: {'ok' if res['ok'] else 'NOT ok'}; {s['errors']} errors, "
            f"{s['warnings']} warnings; {s['planned_pins']} planned pins, {s['pins_in_code']} "
            f"pins configured in code, {s['not_configured_in_code_yet']} not configured in code "
            f"yet, {s['files_scanned']} files scanned. Findings by rule: "
            f"{', '.join(f'{k}={v}' for k, v in s['by_rule'].items()) or 'none'}."), "status"))
        by: dict[str, list[str]] = {}
        for sec in ("plan_findings", "audit_findings", "lint_findings"):
            for f in res.get(sec, []):
                g = f" (GPIO {f['gpio']})" if f.get("gpio") is not None else ""
                by.setdefault(f["rule"], []).append(f"[{f['severity']}] {f['message']}{g}")
        for rule, msgs in sorted(by.items()):
            for i, piece in enumerate(_pieces("\n".join(msgs))):
                out.append(Chunk(f"status:{proj}:finding:{rule}" + (f"-p{i + 1}" if i else ""),
                                 src, f"{rule} findings", _clip(f"{rule} findings:\n{piece}"),
                                 "status"))
        for n in res.get("notes", []):
            notes.append(f"audit: {n}")
    try:
        ev = evidence_mod.build(cfg_path)
    except Exception as e:  # noqa: BLE001
        notes.append(f"live evidence unavailable: {e}")
        ev = None
    if ev:
        snap = str(ev.get("snapshot", {}).get("sha256", ""))[:8]
        src = f"{proj} evidence matrix (snapshot {snap})"
        sm = ev["summary"]
        out.append(Chunk(f"status:{proj}:evidence", src, f"{proj} evidence summary", _clip(
            f"Evidence matrix: {sm['total']} requirements; " + ", ".join(
                f"{sm[k]} {k}" for k in ("pass", "fail", "missing", "stale", "accepted"))), "status"))
        for r in ev["rows"]:
            types = ", ".join(f"{e['type']}={e['result']}" for e in r.get("evidence", [])) or "none"
            cov = ", ".join(r.get("covered_by", [])) or "none"
            out.append(Chunk(f"req:{proj}:{r['req']}", src, f"requirement {r['req']}", _clip(
                f"Requirement {r['req']} ({r.get('kind', '')}): {r['text']} Status {r['status']}, "
                f"strength {r['strength']}. Accepted evidence types: "
                f"{', '.join(r.get('accepts', [])) or 'none'}. Recorded evidence: {types}. "
                f"Covered by tests: {cov}."), "requirement"))
        for n in ev.get("notes", []):
            notes.append(f"evidence: {n}")
    out += _phase_chunks(cfg, proj, notes)
    return out


def _phase_chunks(cfg: dict, proj: str, notes: list[str]) -> list[Chunk]:
    try:
        from cnlp import phases as phases_mod
    except Exception:  # noqa: BLE001  (module may not exist yet)
        return []
    try:
        proc = phases_mod.load(Path(str(cfg["target"])).expanduser(),
                               str(cfg.get("procedure", phases_mod.DEFAULT_PROCEDURE)))
    except Exception as e:  # noqa: BLE001
        notes.append(f"phases unavailable: {e}")
        return []
    if not proc or not proc.get("phases"):
        notes.append("build procedure phases not available")
        return []
    ph, cur = proc["phases"], proc.get("current")
    src = f"{proj} build procedure"
    lines = [f"Phase {p['n']} {p['name']}: {p['status']}" for p in ph]
    head = (f"Current phase: {cur['n']} {cur['name']}. " if cur else "All phases done. ")
    out = [Chunk(f"status:{proj}:phase", src, f"{proj} build phase",
                 _clip(head + "Phases: " + "; ".join(lines)), "status")]
    for p in ph:
        out.append(Chunk(f"status:{proj}:phase{p['n']}", src, f"phase {p['n']} {p['name']}",
                         _clip(f"Phase {p['n']} {p['name']} ({p['status']}). Outcome: "
                               f"{p.get('outcome', '')}. Goal: {p.get('goal', '')} Check: "
                               f"{p.get('check', '')} GPIOs: "
                               f"{', '.join(map(str, p.get('gpios', []))) or 'none'}. Requirements: "
                               f"{', '.join(p.get('reqs', [])) or 'none'}."), "status"))
        if p.get("common_problems"):
            out.append(Chunk(f"status:{proj}:phase{p['n']}-watch", src,
                             f"phase {p['n']} {p['name']} watch-list",
                             _clip(f"Phase {p['n']} {p['name']} watch-list: what to watch out for, "
                                   "common problems and mistakes in this phase. "
                                   + ("This is the CURRENT phase. " if cur and cur["n"] == p["n"]
                                      else "")
                                   + "; ".join(map(str, p["common_problems"])) + "."), "status"))
    return out


# ---------------------------------------------------------------- retrieval

_STOP = frozenset(["a", "an", "the", "i", "me", "my", "we", "you", "it", "is", "are", "be", "do", "does", "did", "can", "how", "why", "what", "when", "where", "which", "who", "to", "of", "in", "on", "for", "and", "or", "if", "so", "t", "s", "with", "this", "that", "there", "should", "would", "could", "not", "no", "am", "was", "were", "has", "have", "had", "will", "about", "as", "at", "by", "from", "into", "than", "then", "use", "get", "want", "need"])
_GPIO_SP = re.compile(r"\bgpio[\s_-]*(\d+)\b", re.IGNORECASE)


def _gpio_set(text: str) -> set[int]:
    """GPIO numbers named in `text`: "GPIO 35", "GPIO 34-39", "GPIOs 12, 13 and 14"."""
    out: set[int] = set()
    for m in _GPIO_LIST.finditer(text):
        for a, b in re.findall(r"(\d+)(?:\s*[-–]\s*(\d+))?", m.group(1)):
            lo, hi = int(a), int(b) if b else int(a)
            out.update(range(lo, hi + 1) if hi >= lo and hi - lo < 64 else [lo])
    return out


def _tokens(text: str) -> list[str]:
    text = text.lower()
    extra = " ".join(f"gpio{g}" for g in sorted(_gpio_set(text)))
    text = _GPIO_SP.sub(lambda m: f" gpio{m.group(1)} {m.group(1)} ", text) + " " + extra
    out: list[str] = []
    for w in re.findall(r"[a-z0-9_]+", text):
        subs = [w]
        if "_" in w:
            subs += [p for p in w.split("_") if p]
        for t in subs:
            if t in _STOP:
                continue
            if len(t) > 4 and t.endswith("ies"):
                t = t[:-3] + "y"  # priorities -> priority
            elif len(t) > 3 and t.endswith("s") and not t.endswith("ss") and not t[-2].isdigit():
                t = t[:-1]
            out.append(t)
    return out


class Index:
    K1, B = 1.4, 0.75

    def __init__(self, chunks: list[Chunk], notes: list[str] | None = None):
        self.chunks = chunks
        self.notes = notes if notes is not None else []
        self._by_id = {c.id: c for c in chunks}
        self._tf = [Counter(_tokens(c.title + " " + c.title + " " + c.text)) for c in chunks]
        self._len = [sum(t.values()) for t in self._tf]
        self._avg = (sum(self._len) / len(chunks)) if chunks else 1.0
        df: Counter[str] = Counter()
        for t in self._tf:
            df.update(t.keys())
        n = len(chunks)
        self._idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}
        self._title_toks = [set(_tokens(c.title)) - {"gpio"} - {t for t in _tokens(c.title)
                                                                 if t.isdigit()} for c in chunks]
        self._cmds = sorted((c for c in chunks if c.kind == "command"),
                            key=lambda c: -len(c.id))

    def get(self, chunk_id: str) -> Chunk | None:
        return self._by_id.get(chunk_id)

    def _named_commands(self, q: str) -> list[Chunk]:
        low = q.lower()
        found: list[Chunk] = []
        for c in self._cmds:
            name = c.id[4:]
            variants = {name, name.replace("-", " ")}
            if any(re.search(rf"(?<![\w-]){re.escape(v)}(?![\w-])", low) for v in variants):
                found.append(c)
        return found

    def retrieve(self, question: str, k: int = 8) -> list[Chunk]:
        q = _tokens(question)
        qset = Counter(q)
        ids = {m.lower() for m in re.findall(r"\b(?:RT\d|[CWRF]\d+)\b", question, re.IGNORECASE)}
        gpios = {m for m in _GPIO_SP.findall(question)}
        how = bool(re.match(r"\s*(?:how|which command|what command)\b", question, re.IGNORECASE))
        scores: list[float] = []
        for i, c in enumerate(self.chunks):
            tf, dl, s = self._tf[i], self._len[i], 0.0
            for w in qset:
                f = tf.get(w, 0)
                if f:
                    s += self._idf[w] * f * (self.K1 + 1) / (
                        f + self.K1 * (1 - self.B + self.B * dl / self._avg))
            if s > 0 and how and c.kind == "command":
                s *= 1.3  # "how do I ..." questions are usually about a command
            if s > 0 and c.kind == "rule" and not how:
                s *= 1.5  # board-concept questions ("no pull", "input-only") want the rule text
            if s > 0 and c.kind == "pin" and not gpios and not (qset.keys() & self._title_toks[i]):
                s *= 0.5  # a pin chunk only matters if the question names that pin or its role
            if s > 0 or ids:
                tail = c.id.split(":", 2)[-1].lower()
                head = tail.split("_")[0]
                if c.kind in ("rule", "requirement", "pin") and (tail in ids or head in ids):
                    s += 12.0
                if c.kind == "pin" and tail.startswith("gpio") and tail[4:] in gpios:
                    s += 3.0
                if c.kind == "rule" and gpios & {str(g) for g in _gpio_set(c.text)}:
                    s += 4.0
            scores.append(s)
        order = sorted(range(len(self.chunks)), key=lambda i: (-scores[i], i))
        top = [self.chunks[i] for i in order if scores[i] > 0][:k]
        named = [c for c in self._named_commands(question) if c not in top]
        if named:
            top = top[: max(k - len(named), 0)] + named
        return top[:k]


def build_index(cfg_path: Path | None = None, *, live: bool = True) -> Index:
    notes: list[str] = []
    chunks = _doc_chunks(notes) + _module_doc_chunks(notes) + _command_chunks() + _rule_chunks(notes)
    if cfg_path is not None:
        cfg_path = Path(cfg_path).expanduser()
        try:
            cfg = yaml.safe_load(cfg_path.read_text("utf-8")) or {}
            base = cfg_path.parent
            proj = base.name
            chunks += _spec_chunks(base / str(cfg.get("spec", "spec.cnl")), proj, notes)
            chunks += _param_chunks(base, cfg, proj, notes)
            live_chunks: list[Chunk] = []
            if live:
                try:
                    live_chunks = _live_chunks(cfg_path, proj, notes)
                except Exception as e:  # noqa: BLE001
                    notes.append(f"live project data unavailable: {e}")
            have = {c.id for c in live_chunks}
            chunks += [c for c in _req_meta_chunks(base, proj) if c.id not in have]
            chunks += live_chunks
        except Exception as e:  # noqa: BLE001
            notes.append(f"project knowledge unavailable for {cfg_path}: {e}")
    seen: set[str] = set()
    uniq = []
    for c in chunks:
        if c.id in seen:
            continue
        seen.add(c.id)
        uniq.append(c)
    return Index(uniq, notes)


# ---------------------------------------------------------------- facts

_GPIO_LIST = re.compile(
    r"gpios?\s*(\d+(?:\s*[-–]\s*\d+)?(?:\s*(?:,|/|and|or)\s*(?:and\s+|or\s+)?"
    r"\d+(?:\s*[-–]\s*\d+)?)*)", re.IGNORECASE)
_RULE_ID = re.compile(r"\b(?:C\d+|W\d+|RT\d+(?:_[A-Z0-9]+)*|(?:LINT|AUDIT|STATUS|READBACK|PARAM|FSM)_[A-Z_]+|"
                      r"OWNER|OPEN_DRAIN_PULL|I2C_WEAK_PULL|FLOATING)\b")
_REQ_ID = re.compile(r"\b[RF]\d+\b")
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")


def facts(chunks: list[Chunk]) -> dict:
    gpios: set[int] = set()
    rules: set[str] = set()
    reqs: set[str] = set()
    cmds: set[str] = set()
    nums: set[str] = set()
    names = [c["name"] for c in commands()]
    for c in chunks:
        t = c.text + " " + c.title
        gpios.update(_gpio_set(t))
        rules.update(_RULE_ID.findall(t))
        reqs.update(_REQ_ID.findall(t))
        nums.update(_NUM.findall(t))
        low = t.lower()
        cmds.update(n for n in names
                    if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", low))
        if c.kind == "command":
            cmds.add(c.id[4:].replace("-", " "))
        elif c.kind == "rule":
            rules.add(c.id.split(":", 1)[1])
        elif c.kind == "requirement":
            reqs.add(c.id.rsplit(":", 1)[1])
        elif c.kind == "pin":
            m = re.search(r"gpio(\d+)$", c.id)
            if m:
                gpios.add(int(m.group(1)))
    nums.update(str(g) for g in gpios)
    return {"gpios": gpios, "rule_ids": rules, "req_ids": reqs, "commands": cmds,
            "numbers": nums}
