"""`cnlp ask`: a guarded natural-language help desk.

The local model reads numbered sources (retrieved from knowledge.py) and answers with a
JSON object: a suggested cnlp command, a cited answer, or "unknown". Everything the model
says is then checked deterministically: commands must parse with the real CLI parser and
contain no shell operators, answers may only mention GPIOs, rule IDs, requirement IDs and
commands that appear in the retrieved sources, and pass/fail claims need evidence in the
sources. Commands are only ever run if they are read-only and the caller asked for it.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shlex
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cnlp import llm

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["command", "answer", "unknown"]},
        "command": {"type": ["string", "null"]},
        "why": {"type": "string"},
        "answer": {"type": ["string", "null"]},
        "sources": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["kind", "command", "why", "answer", "sources", "confidence"],
}

SYSTEM_PROMPT = """You are the help desk for cnlp, a command-line tool that checks an ESP32 \
project's pins, rules, evidence and build status. Reply with one JSON object only:
{"kind": "command"|"answer"|"unknown", "command": string|null, "why": string, \
"answer": string|null, "sources": [source ids], "confidence": "high"|"medium"|"low"}

Rules:
- Answer ONLY from the numbered sources below. If they do not cover the question, or it is \
not about cnlp or the user's project, use kind "unknown" (command and answer null).
- If the user wants to DO something (check, audit, measure, see status), prefer kind \
"command": "command" is one full cnlp command line starting with `cnlp`, no pipes, `;`, `&&`, \
redirects or substitutions, using only commands and options listed under COMMANDS. "why" is \
one short sentence.
- Otherwise kind "answer": 1 to 4 short sentences in "answer". Cite the source ids you used in \
"sources".
- Never invent GPIO numbers, rule IDs, requirement IDs, measurements or results. Only mention \
ones that appear in the sources.
- Never claim a requirement passed, or that a measurement was taken, unless a source says so. \
Nothing is passed until the user reports a measurement.
- Never write firmware code for the user's project; they write it themselves in small reviewed \
steps. Explain the concept instead.
- cnlp never flashes, approves gates or records evidence on its own: the user runs those.
"""

# characters that could chain, redirect or substitute in a shell
SHELL_CHARS = set(";|&$`><\n")

# commands that change state or touch hardware: never run by `ask --run`
NEVER_RUN = {"flash", "measure", "gen", "spec", "sync", "gate", "ask"}


@dataclass
class AskResult:
    question: str = ""
    kind: str = "unknown"  # command | answer | unknown
    command: str | None = None
    why: str = ""
    answer: str | None = None
    sources: list[str] = field(default_factory=list)
    confidence: str = "low"
    read_only: bool = False
    grounded: bool = True
    unsupported: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fallback: bool = False
    refused: bool = False
    ran: Any = None
    ran_summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _kb():
    from cnlp import knowledge
    return knowledge


# --- refusals ------------------------------------------------------------------------

_LEAD = r"(?:^|\b(?:please|can you|could you|would you|just|go ahead and|now|and)\s+)"
REFUSALS = [
    ("efuse", re.compile(r"\be-?fuses?\b.*\b(burn|blow|set|write|program)|\b(burn|blow)\b.*\be-?fuse",
                         re.IGNORECASE)),
    ("flash", re.compile(_LEAD + r"(flash|burn)\b", re.IGNORECASE)),
    ("gate", re.compile(_LEAD + r"(approve|sign off)\b|\bapprove (the |all |every )?gates?\b", re.IGNORECASE)),
    ("evidence", re.compile(_LEAD + r"(record|log|add)\b.{0,30}\bevidence\b"
                            r"|\bmark\b.{0,25}\b(R\d+|F\d+|requirement|it|gate)\b.{0,15}\b(pass|passed|done|verified)",
                            re.IGNORECASE)),
    ("code", re.compile(r"\b(write|generate|implement|code|create|give me)\b.{0,50}"
                        r"(\bscan\.c\b|\bfirmware\b|\bmy (code|driver|task|scan|audio|display|\w+\.c)\b"
                        r"|\bthe code\b|\.c\b)", re.IGNORECASE)),
]


def _refusal_kind(q: str) -> str | None:
    for name, rx in REFUSALS:
        if rx.search(q):
            return name
    return None


def _command_info(kb, name: str) -> dict | None:
    for c in kb.commands():
        if c["name"] == name:
            return c
    return None


def _refuse(kind: str, question: str, kb, index) -> AskResult:
    r = AskResult(question=question, refused=True, confidence="high", read_only=False)
    cmds = {"flash": "flash", "gate": "gate", "evidence": "evidence"}
    if kind in cmds:
        name = cmds[kind]
        usage = {"flash": "cnlp flash --port /dev/ttyUSB0", "gate": "cnlp gate review",
                 "evidence": "cnlp evidence add <project> <REQ> <type> pass|fail "
                             "--value <v> --criterion <c>"}[kind]
        r.kind, r.command = "command", usage
        what = {"flash": "flash the board", "gate": "approve a gate",
                "evidence": "record evidence"}[kind]
        r.why = (f"cnlp will not {what} for you. Run this yourself when you are ready"
                 + (", after you have measured." if kind == "evidence" else "."))
        sid = f"cmd:{name}"
        if index is not None and index.get(sid) is not None:
            r.sources = [sid]
    elif kind == "efuse":
        r.kind = "answer"
        r.answer = ("cnlp never burns efuses; they cannot be undone, so that stays a manual "
                    "decision outside this tool.")
    else:
        r.kind = "answer"
        r.answer = ("cnlp will not write your firmware. You write it in small reviewed steps; "
                    "ask me to explain the concept, or ask your coding agent in the project to "
                    "help one small change at a time.")
    r.notes.append("refused: cnlp does not do this for you")
    return r


# --- prompting ------------------------------------------------------------------------

def _build_messages(question: str, chunks, commands: list[dict]) -> list[dict]:
    src = "\n\n".join(f"[{c.id}] {c.source} — {c.title}\n{c.text[:1200]}" for c in chunks)
    cmds = "\n".join(f"- {c['name']}: {c.get('usage', '')} — {c.get('help', '')}"
                     f"{'' if c.get('read_only') else ' (changes state)'}" for c in commands)
    user = f"SOURCES\n{src or '(none)'}\n\nCOMMANDS\n{cmds}\n\nQUESTION\n{question}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def _parse_reply(text: str) -> dict:
    d = json.loads(text)
    if not isinstance(d, dict):
        raise TypeError("reply is not a JSON object")
    kind = d.get("kind")
    if kind not in ("command", "answer", "unknown"):
        raise ValueError(f"bad kind {kind!r}")
    d.setdefault("command", None)
    d.setdefault("answer", None)
    d["why"] = str(d.get("why") or "")
    d["sources"] = [s.strip().strip("[]").strip() for s in (d.get("sources") or [])
                    if isinstance(s, str)]
    d["confidence"] = d.get("confidence") if d.get("confidence") in ("high", "medium", "low") else "low"
    return d


# --- validation -----------------------------------------------------------------------

def validate_command(cmd: str | None) -> tuple[list[str] | None, str]:
    """(argv after 'cnlp', error). argv is None when the command is not acceptable."""
    if not cmd or not cmd.strip():
        return None, "no command given"
    if "\n" in cmd or "`" in cmd or "$(" in cmd:
        return None, "contains shell operators"
    try:
        # Operators only count outside quotes: `--criterion "< 1.6 ms"` is fine, `> out.txt` is not.
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
        parts = shlex.split(cmd)
    except ValueError as e:
        return None, f"cannot split: {e}"
    if any(t and set(t) <= SHELL_CHARS for t in tokens):
        return None, "contains shell operators"
    if not parts or parts[0] != "cnlp":
        return None, "must start with cnlp"
    argv = parts[1:]
    if not argv:
        return None, "no subcommand"
    from cnlp import cli
    if argv[-1] in ("-h", "--help"):  # help is fine as long as the subcommand exists
        argv = argv[:-1]
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                cli.build_parser().parse_args([*argv, "-h"])
        except SystemExit as e:
            return (parts[1:], "") if e.code == 0 else (None, "does not parse as a cnlp command")
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            cli.build_parser().parse_args(argv)
    except SystemExit:
        return None, "does not parse as a cnlp command"
    return argv, ""


def _is_read_only(kb, argv: list[str]) -> bool:
    if argv[0] in NEVER_RUN:
        return False
    toks = [a for a in argv if not a.startswith("-")]
    if argv[0] == "evidence" and "add" in toks[:2]:
        return False
    if "--save" in argv or "--apply" in argv or "--record" in argv:
        return False
    info = None
    for n in (" ".join(toks[:2]), toks[0] if toks else ""):
        info = _command_info(kb, n)
        if info:
            break
    return bool(info and info.get("read_only"))


GPIO_RX = re.compile(r"\b(?:GPIO|pins?)[\s_#-]*(\d{1,2})(?:\s*(?:-|–|—|to)\s*(\d{1,2}))?\b", re.IGNORECASE)
RULE_RX = re.compile(r"\b(C\d+|W\d+|RT\d+_[A-Z0-9_]+|AUDIT_[A-Z0-9_]+|LINT_[A-Z0-9_]+"
                     r"|FSM_[A-Z0-9_]+|OWNER)\b")
REQ_RX = re.compile(r"\b([RF]\d+)\b")
CMD_RX = re.compile(r"(?:`\s*|\b(?:run|use|try|with)\s+|^\s*)cnlp\s+([a-z][a-z-]+)", re.IGNORECASE | re.MULTILINE)
PASS_RX = re.compile(r"\b(passed|passes|passing|meets?|met|verified|satisf(?:ies|ied)|complete[sd]?)\b", re.IGNORECASE)
NEG_RX = re.compile(r"\b(not|no|n't|never|nothing|yet|until|unless|if|whether|hasn|haven|isn|aren)\b", re.IGNORECASE)


def find_claims(text: str) -> dict:
    gpios: set[int] = set()
    for a, b in GPIO_RX.findall(text):
        lo, hi = int(a), int(b) if b else int(a)
        gpios.update(range(lo, min(hi, lo + 40) + 1) if hi >= lo else {lo})
    return {
        "gpios": gpios,
        "rule_ids": set(RULE_RX.findall(text)),
        "req_ids": set(REQ_RX.findall(text)),
        "commands": {m.lower() for m in CMD_RX.findall(text)},
    }


def pass_claims(text: str, chunks) -> list[str]:
    """Sentences that say a requirement passed/was verified without an evidence source saying so."""
    evidence = [c for c in chunks if c.kind == "evidence" or c.id.startswith(("evidence", "ev:"))]
    bad = []
    for sent in re.split(r"(?<=[.!?])\s+|\n", text):
        reqs = REQ_RX.findall(sent)
        if not reqs or not PASS_RX.search(sent) or NEG_RX.search(sent):
            continue
        for rq in reqs:
            ok = any(rq in c.text and re.search(r"\bpass(ed)?\b", c.text, re.IGNORECASE) for c in evidence)
            if not ok:
                bad.append(f"pass claim for {rq}")
    return bad


def check_grounding(answer: str, kb, chunks) -> list[str]:
    facts = kb.facts(chunks)
    claims = find_claims(answer)
    bad: list[str] = []
    bad += [f"GPIO {n}" for n in sorted(claims["gpios"] - set(facts.get("gpios", ())))]
    bad += sorted(claims["rule_ids"] - set(facts.get("rule_ids", ())))
    bad += sorted(claims["req_ids"] - set(facts.get("req_ids", ())))
    bad += [f"cnlp {c}" for c in sorted(claims["commands"] - set(facts.get("commands", ())))]
    bad += pass_claims(answer, chunks)
    return bad


# --- main -----------------------------------------------------------------------------

def _unknown(r: AskResult, why: str) -> AskResult:
    r.kind, r.command, r.answer, r.sources = "unknown", None, None, []
    r.confidence, r.read_only = "low", False
    r.notes.append(why)
    return r


def _fallback(question: str, kb, chunks, err: Exception) -> AskResult:
    r = AskResult(question=question, fallback=True, confidence="low")
    r.notes.append(f"model unavailable ({err}); suggesting the closest command by keyword")
    top = next((c for c in chunks if c.id.startswith("cmd:")), None)
    info = _command_info(kb, top.id[4:]) if top else None
    if not info:
        return _unknown(r, "no matching command found")
    # Usage lines have placeholders that don't parse; point at the command's own help.
    cmd = f"cnlp {info['name']} --help"
    argv, bad = validate_command(cmd)
    if argv is None:
        return _unknown(r, f"closest command is not usable: {bad}")
    r.kind, r.command, r.sources = "command", cmd, [top.id]
    r.why = info.get("help", "")
    r.read_only = _is_read_only(kb, argv)
    return r


def ask(question: str, cfg_path: Path | None = None, *, run: bool = False, client=llm,
        index=None) -> AskResult:
    kb = _kb()
    index = index if index is not None else kb.build_index(cfg_path)
    question = question.strip()
    chunks = index.retrieve(question, k=8)
    ids = {c.id for c in chunks}

    refusal = _refusal_kind(question)
    if refusal:
        r = _refuse(refusal, question, kb, index)
        if r.command:
            argv, _ = validate_command(r.command)
            if argv is None:  # usage line with placeholders may not parse; still show it
                r.notes.append("usage shown as a template")
        return r

    messages = _build_messages(question, chunks, kb.commands())
    r = AskResult(question=question)
    d: dict | None = None
    last_err = ""
    for attempt in range(2):
        try:
            text = client.complete(messages, temperature=0.2, max_tokens=700, json_schema=SCHEMA)
        except llm.LlmError as e:
            return _fallback(question, kb, chunks, e)
        try:
            d = _parse_reply(text)
        except (ValueError, TypeError) as e:
            last_err = f"the reply was not valid ({e})"
        else:
            if d["kind"] == "command":
                argv, bad = validate_command(d.get("command"))
                if argv is None:
                    last_err = f"the command {d.get('command')!r} was rejected: {bad}"
                    r.notes.append(last_err)
                    d = None
                else:
                    r.read_only = _is_read_only(kb, argv)
            if d is not None:
                break
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": f"That was rejected: {last_err}. Reply again with "
             "valid JSON, using only a command from the COMMANDS list."}]
    if d is None:
        return _unknown(r, f"no usable reply after a retry: {last_err}")

    r.kind, r.why, r.confidence = d["kind"], d["why"], d["confidence"]
    kept = [s for s in dict.fromkeys(d["sources"]) if s in ids]
    if len(kept) != len(d["sources"]):
        r.notes.append("dropped sources that were not retrieved: "
                       + ", ".join(s for s in d["sources"] if s not in ids))
    r.sources = kept
    if r.kind == "command":
        r.command = d["command"].strip()
    elif r.kind == "answer":
        r.answer = (d.get("answer") or "").strip()
        if not r.answer:
            return _unknown(r, "empty answer")
        bad = check_grounding(r.answer, kb, chunks)
        if bad:
            r.unsupported = bad
            r.grounded = False
            r.confidence = "low"
            if len(bad) > 2 or any(b.startswith("pass claim") for b in bad):
                return _unknown(r, "answer withheld, not supported by sources: " + ", ".join(bad))
            r.answer = "Warning, parts of this are not in my sources. " + r.answer
    else:
        r.answer = r.command = None
        r.sources = []
    if r.kind != "command":
        r.read_only = False

    if run and r.kind == "command":
        _maybe_run(r)
    elif r.kind == "command" and not r.read_only:
        r.notes.append("this changes state or touches hardware; run it yourself")
    return r


def _maybe_run(r: AskResult) -> None:
    argv, _ = validate_command(r.command)
    if argv is None or not r.read_only:
        r.notes.append("not run: only read-only commands are run for you; run it yourself")
        return
    from cnlp import cli
    out, err = io.StringIO(), io.StringIO()
    argv = argv + ([] if "--json" in argv else ["--json"])
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 2
    except Exception as e:  # noqa: BLE001  a read-only command crashing should not break ask
        r.notes.append(f"command failed: {e}")
        return
    text = out.getvalue().strip()
    try:
        r.ran = json.loads(text)
    except json.JSONDecodeError:
        r.ran = {"stdout": text, "stderr": err.getvalue().strip()}
    r.ran_summary = _summarize(r.ran, code)


def _summarize(data: Any, code: int) -> str:
    if isinstance(data, dict):
        bits = [f"exit {code}"]
        for k in ("ok", "phase", "verdict", "summary", "error"):
            if k in data and not isinstance(data[k], (dict, list)):
                bits.append(f"{k}: {data[k]}")
            elif k in data and isinstance(data[k], dict):
                bits.append(f"{k}: " + ", ".join(f"{a}={b}" for a, b in list(data[k].items())[:6]
                                                  if not isinstance(b, (dict, list))))
        return "; ".join(bits)
    return f"exit {code}"


# --- text output ------------------------------------------------------------------------

def format_text(result: AskResult, style) -> str:
    r = result
    w = style.width
    arrow = "→" if style.uni else "->"
    out: list[str] = []

    def wrap(text: str, indent: str = "  ") -> str:
        return textwrap.fill(text, w, initial_indent=indent, subsequent_indent=indent)

    if r.kind == "command":
        out.append(f"{style.c('bold', arrow)} {r.command}")
        if r.why:
            out.append(wrap(r.why))
        if r.refused:
            pass
        elif not r.read_only:
            out.append(wrap("Changes state or touches hardware: run it yourself."))
    elif r.kind == "answer":
        out.append(textwrap.fill(r.answer or "", w))
    else:
        out.append(f"{style.c('dim', '?')} I don't know {style.dash} my sources don't cover that. "
                   "Try: cnlp --help")
    if r.sources:
        out.append(f"  sources: {', '.join(r.sources)}")
    if r.unsupported:
        out.append(style.c("yellow", f"{style.warn_sym} unsupported: {', '.join(r.unsupported)}"
                           " (not in my sources; double-check)"))
    if r.fallback:
        out.append(style.c("dim", "  (offline: closest command by keyword)"))
    for n in r.notes:
        out.append(style.c("dim", wrap(n)))
    if r.ran is not None:
        out.append(f"  ran: {r.ran_summary}")
    return "\n".join(out)
