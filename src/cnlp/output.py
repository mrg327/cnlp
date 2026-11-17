"""Human-readable output for `cnlp` commands.

One formatter per command; each takes the same dict the command emits as JSON and
returns text. Colour only on a TTY with NO_COLOR unset; ASCII symbols when the
terminal encoding cannot print the nicer ones.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

_ANSI = {"red": "31", "yellow": "33", "green": "32", "dim": "2", "bold": "1"}
_LOC = re.compile(r"^(\S+?:\d+(?::\d+)?:)\s+(.*)$", re.DOTALL)


class Style:
    def __init__(self, stream=None, width: int | None = None, color: bool | None = None,
                 project: str = ".") -> None:
        stream = stream or sys.stdout
        if color is None:
            color = bool(getattr(stream, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ
        self.color = color
        self.width = width or shutil.get_terminal_size((100, 24)).columns
        self.project = rel(project) if project not in (".", "") else "."
        enc = getattr(stream, "encoding", None) or "ascii"
        try:
            "✓✗—…".encode(enc)
            self.uni = True
        except (UnicodeEncodeError, LookupError):
            self.uni = False
        self.ok_sym, self.err_sym, self.warn_sym = (
            ("✓", "✗", "!") if self.uni else ("ok", "x", "!"))
        self.dash = "—" if self.uni else "-"
        self.ell = "…" if self.uni else "..."

    def c(self, name: str, text: str) -> str:
        return f"\x1b[{_ANSI[name]}m{text}\x1b[0m" if self.color else text

    def sym(self, sev: str) -> str:
        return {"error": self.c("red", self.err_sym), "warning": self.c("yellow", self.warn_sym),
                "ok": self.c("green", self.ok_sym)}[sev]


def rel(path: Any) -> str:
    """Path relative to cwd when that is shorter."""
    p = str(path)
    try:
        r = os.path.relpath(p)
    except ValueError:
        return p
    return r if Path(p).is_absolute() and len(r) < len(p) else p


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _wrap(prefix_plain: str, prefix: str, text: str, width: int) -> str:
    """Wrap `text` after `prefix`, hanging-indented to the visible prefix width."""
    pad = " " * len(prefix_plain)
    lines = textwrap.wrap(text, max(width - len(prefix_plain), 20), break_long_words=True,
                          break_on_hyphens=False) or [""]
    return "\n".join((prefix if i == 0 else pad) + ln for i, ln in enumerate(lines))


def counts_line(findings: list[dict]) -> str:
    e = sum(f.get("severity") == "error" for f in findings)
    w = sum(f.get("severity") == "warning" for f in findings)
    parts = []
    if e:
        parts.append(plural(e, "error"))
    if w:
        parts.append(plural(w, "warning"))
    return ", ".join(parts)


def finding_lines(findings: list[dict], st: Style, indent: str = "  ") -> list[str]:
    """Errors first, then warnings; rule ID in a fixed-width column."""
    order = {"error": 0, "warning": 1}
    fs = sorted(findings, key=lambda f: order.get(f.get("severity"), 2))
    col = max([len(str(f.get("rule", ""))) for f in fs] + [4])
    out = []
    for f in fs:
        sev = f.get("severity", "warning")
        rule = str(f.get("rule", "")).ljust(col)
        plain = f"{indent}x {rule}  "
        msg = str(f.get("message", ""))
        m = _LOC.match(msg)
        loc = ""
        if m:
            loc, msg = m.group(1), m.group(2)
            msg_text = f"{loc} {msg}"
        else:
            msg_text = msg
        wrapped = _wrap(plain, f"{indent}{st.sym(sev)} {rule}  ", msg_text, st.width)
        if loc:
            wrapped = wrapped.replace(loc, st.c("dim", loc), 1)
        out.append(wrapped)
    return out


def _verdict(findings: list[dict], st: Style, ok_label: str) -> str:
    counts = counts_line(findings)
    errors = any(f.get("severity") == "error" for f in findings)
    if errors:
        return f"{st.sym('error')} {counts}"
    if not counts:
        return f"{st.sym('ok')} {ok_label} OK {st.dash} no findings"
    return f"{st.sym('ok')} {ok_label} OK {st.dash} {counts}"


def _findings_report(obj: dict, st: Style, label: str) -> str:
    fs = obj.get("findings", [])
    lines = [_verdict(fs, st, label)]
    lines += finding_lines(fs, st)
    return "\n".join(lines)


def fmt_findings(label: str):
    return lambda obj, st: _findings_report(obj, st, label)


def table(rows: list[list[str]], header: list[str], indent: str = "  ") -> list[str]:
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    out = []
    for r in [header] + rows:
        out.append(indent + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    return out


# ---- per command ---------------------------------------------------------------------------

def fmt_error(obj: dict, st: Style) -> str:
    return f"{st.sym('error')} {obj.get('error') or obj.get('message') or 'failed'}"


def fmt_translate(obj: dict, st: Style) -> str:
    if not obj.get("ok"):
        errs = obj.get("errors", [])
        lines = [f"{st.sym('error')} {plural(len(errs), 'parse error')}"]
        for e in errs:
            pos = f"{e.get('line', '?')}:{e.get('column', '?')}"
            lines.append(_wrap(f"  {pos:<7} ", f"  {st.c('dim', pos.ljust(7))} ",
                               str(e.get("message", "")), st.width))
        return "\n".join(lines)
    lines = [f"{st.sym('ok')} Translated {st.dash} manifest at {rel(obj['manifest'])}",
             f"  round trip: {'ok' if obj.get('round_trip_ok') else 'DIFFERS'}"]
    assume = obj.get("assumptions", [])
    if assume:
        lines.append(f"\nAssumptions the spec never stated ({len(assume)}):")
        for i, a in enumerate(assume, 1):
            head = f"  {i}. "
            lines.append(_wrap(head, head, f"{a.get('field')} = {a.get('value')}: "
                               f"{a.get('question', '')}", st.width))
    return "\n".join(lines)


def fmt_gen(obj: dict, st: Style) -> str:
    if not obj.get("ok"):
        fs = obj.get("findings", [])
        return "\n".join([f"{st.sym('error')} {obj.get('reason', 'generation refused')}",
                          *finding_lines(fs, st)])
    files = obj.get("files", [])
    lines = [f"{st.sym('ok')} Generated {plural(len(files), 'file')}"]
    for f in files:
        lines.append(f"  {f['path']}  {st.c('dim', str(f.get('sha256', ''))[:8])}")
    return "\n".join(lines)


def fmt_gate_next(obj: dict | None, st: Style) -> str:
    if not obj:
        return f"{st.sym('ok')} Nothing is waiting for approval"
    lines = [f"{st.c('bold', obj['title'])}  {st.c('dim', '(' + obj['id'] + ')')}", ""]
    for ln in str(obj.get("summary", "")).splitlines():
        lines.append("  " + ln if ln.strip() else "")
    lines.append("")
    if obj.get("blocked"):
        lines.append(f"{st.sym('error')} blocked: {obj.get('reason', 'not ready')}")
        return "\n".join(lines)
    proj = f"--project {st.project} " if st.project not in (".", "") else ""
    lines.append("To approve, run:")
    lines.append(f'  cnlp {proj}gate answer {obj["id"]} {obj["hash"]} approve "your note"')
    lines.append(f"or review interactively with `cnlp {proj}gate review`.")
    return "\n".join(lines)


def fmt_gate_answer(obj: dict, st: Style) -> str:
    return f"{st.sym('ok' if obj.get('ok') else 'error')} {obj.get('message', '')}"


def fmt_sync(obj: dict, st: Style) -> str:
    msg = obj.get("message")
    edited, missing = obj.get("edited", []), obj.get("missing", [])
    props, unrec = obj.get("proposals", []), obj.get("unrecognized", [])
    if obj.get("ok") and not edited and not missing and not props:
        return f"{st.sym('ok')} {msg or 'Generated files match the spec: no hand edits'}"
    lines = [f"{st.sym('ok' if obj.get('ok') else 'error')} "
             + (msg if msg and not (edited or missing) else
                f"{plural(len(edited), 'file')} edited by hand, {len(missing)} missing")]
    for f in edited:
        lines.append(f"  edited   {f}")
    for f in missing:
        lines.append(f"  missing  {f}")
    if props:
        lines.append(f"\nProposed spec changes ({len(props)}):")
        for p in props:
            lines.append(f"  {st.c('dim', p['file'])}")
            if p.get("before"):
                lines.append("    " + st.c("red", "- " + p["before"]))
            if p.get("after"):
                lines.append("    " + st.c("green", "+ " + p["after"]))
    if unrec:
        lines.append("\nEdits cnlp does not understand:")
        for u in unrec:
            lines.append(_wrap(f"  {u['file']}: ", f"  {u['file']}: ", u.get("message", ""),
                               st.width))
    if props and not unrec:
        lines.append("\nNext: run `cnlp sync --apply` to write these into spec.cnl.")
    elif unrec:
        lines.append("\nNext: revert those edits or move the change into spec.cnl by hand.")
    if msg and (edited or missing):
        lines.append(msg)
    return "\n".join(lines)


def _mismatch_table(mm: list[dict]) -> list[str]:
    rows = [[str(m["gpio"]), str(m.get("role", "")), str(m["field"]), str(m["expected"]),
             str(m["actual"])] for m in mm]
    return table(rows, ["GPIO", "ROLE", "FIELD", "EXPECTED", "ACTUAL"])


def fmt_flash(obj: dict, st: Style) -> str:
    stage = obj.get("stage", "")
    head = "Flash" if obj.get("ok") else "Flash stopped"
    lines = [f"{st.sym('ok' if obj.get('ok') else 'error')} {head} (stage: {stage})"]
    lines.append(_wrap("  ", "  ", str(obj.get("message", "")), st.width) if "\n" not in
                 str(obj.get("message", "")) else
                 "\n".join("  " + x for x in str(obj["message"]).splitlines()))
    if obj.get("image_sha256"):
        lines.append(f"  image {obj['image_sha256'][:12]}")
    if obj.get("mismatches"):
        lines.append("")
        lines += _mismatch_table(obj["mismatches"])
    return "\n".join(lines)


def fmt_readback(obj: dict, st: Style) -> str:
    mm = obj.get("mismatches", [])
    if not mm:
        return f"{st.sym('ok')} Readback matches the manifest"
    return "\n".join([f"{st.sym('error')} {plural(len(mm), 'mismatch', 'mismatches')}",
                      *_mismatch_table(mm)])


def fmt_spec(obj: dict, st: Style) -> str:
    agreed = obj.get("agreed")
    lines = [f"{st.sym('ok' if obj.get('ok') else 'error')} Wrote {rel(obj.get('spec', ''))}",
             f"  samples agree: {'yes' if agreed else 'no'}"]
    qs = obj.get("questions", [])
    if qs:
        lines.append(f"\nQuestions ({len(qs)}):")
        for i, q in enumerate(qs, 1):
            lines.append(_wrap(f"  {i}. ", f"  {i}. ", str(q), st.width))
    asm = obj.get("assumptions", [])
    if asm:
        lines.append(f"\nAssumptions ({len(asm)}):")
        for a in asm:
            text = (f"{a.get('field')} = {a.get('value')}: {a.get('question', '')}"
                    if isinstance(a, dict) else str(a))
            lines.append(_wrap("  - ", "  - ", text, st.width))
    if obj.get("next"):
        lines.append("\n" + obj["next"])
    return "\n".join(lines)


def fmt_audit(obj: dict, st: Style) -> str:
    s = obj.get("summary", {})
    snap = obj.get("snapshot", {})
    pf, af, lf = (obj.get(k, []) for k in ("plan_findings", "audit_findings", "lint_findings"))
    unused = [f for f in af if f.get("rule") == "AUDIT_UNUSED"]
    af_shown = [f for f in af if f.get("rule") != "AUDIT_UNUSED"]
    prf = obj.get("param_findings") or []
    p_missing = [f for f in prf if f.get("rule") in ("PARAM_MISSING", "PARAM_TASK_MISSING")]
    prf_shown = [f for f in prf if f not in p_missing]
    e, w = s.get("errors", 0), s.get("warnings", 0) - len(unused) - len(p_missing)
    snap_line = (f"Snapshot: {plural(snap.get('files', 0), 'file')}, "
                 f"sha {str(snap.get('sha256', ''))[:8]}{st.ell}")
    lines = [f"Audit of {rel(obj.get('target', ''))}", snap_line, ""]
    parts = ", ".join(x for x in (plural(e, "error") if e else "",
                                  plural(w, "warning") if w else "") if x)
    lines.append(f"{st.sym('error')} {parts}" if e else
                 f"{st.sym('ok')} Audit OK {st.dash} {parts or 'no findings'}")
    for title, fs in (("Plan", pf), ("Code vs plan", af_shown), ("Lint", lf)):
        c = counts_line(fs)
        lines += ["", f"{st.c('bold', title)}  {st.c('dim', c if c else 'clean')}"]
        lines += finding_lines(fs, st)
    if obj.get("params") is not None:
        c = counts_line(prf_shown)
        lines += ["", f"{st.c('bold', 'Parameters')}  {st.c('dim', c if c else 'clean')}"]
        lines += finding_lines(prf_shown, st)
        for f in p_missing:
            lines.append(_wrap("  - ", "    ", str(f.get("message", "")), st.width))
        for k, v in (s.get("derived") or {}).items():
            lines.append(f"  derived: {k} = {v}")
    counts = (f"  {s.get('planned_pins', 0)} planned pins, {s.get('pins_in_code', 0)} found in "
              f"code, {s.get('files_scanned', 0)} files scanned")
    lines += ["", st.c("bold", "Summary"), counts]
    n = s.get("not_configured_in_code_yet", 0)
    if n:
        lines.append(f"  {plural(n, 'planned pin')} not configured yet (expected mid-project)")
    if obj.get("notes"):
        lines += ["", st.c("bold", "Notes")]
        lines += [_wrap("  - ", "  - ", str(x), st.width) for x in obj["notes"]]
    return "\n".join(lines)


def fmt_measure(obj: dict, st: Style) -> str:
    if "reply" in obj:
        return f"{obj['query']} {st.c('dim', '->')} {obj['reply']}"
    from cnlp.measure import KIND_LABEL

    h = obj["human"]
    label = KIND_LABEL.get(obj["kind"], obj["kind"])
    where = f"CH{obj['channel']}" + (f"->CH{obj['channel2']}" if obj.get("channel2") else "")
    line = (f"{label} on {where}: mean {h['mean']} (min {h['min']}, max {h['max']}, "
            f"{'σ' if st.uni else 'sigma'} {h['std']}, n={obj['n']})")
    chk = obj.get("check")
    if chk:
        line += f"  {st.sym('ok' if chk['ok'] else 'error')} " + (
            f"meets \"{chk['criterion']}\"" if chk["ok"] else f"fails \"{chk['criterion']}\"")
    lines = [line]
    rec = obj.get("recorded")
    if rec:
        lines.append(f"Recorded {rec['req']} {rec['type']} {rec['result']}")
    return "\n".join(lines)


def fmt_evidence(obj: dict, st: Style) -> str:
    s = obj.get("summary", {})
    rows = obj.get("rows", [])
    colors = {"pass": "green", "accepted": "green", "fail": "red", "missing": "yellow",
              "stale": "yellow"}
    # Missing evidence is normal mid-project: only failures get the red mark.
    gaps = s.get("missing", 0) + s.get("stale", 0)
    verdict = "error" if s.get("fail", 0) else ("warning" if gaps else "ok")
    lines = []
    if obj.get("target"):
        lines.append(f"Evidence for {rel(obj['target'])}")
    snap = obj.get("snapshot") or {}
    if isinstance(snap, dict) and snap.get("sha256"):
        lines.append(f"Snapshot: {plural(snap.get('files', 0), 'file')}, "
                     f"sha {str(snap['sha256'])[:8]}{st.ell}")
    if lines:
        lines.append("")
    lines.append(f"{st.sym(verdict)} {s.get('pass', 0)} of {s.get('total', 0)} "
                 f"requirements pass; {s.get('fail', 0)} fail, {s.get('missing', 0)} missing, "
                 f"{s.get('stale', 0)} stale, {s.get('accepted', 0)} accepted")
    if rows:
        rw = max([len(str(r.get("req", ""))) for r in rows] + [3])
        sw = max([len(str(r.get("strength", ""))) for r in rows] + [8], default=8)
        sw = max(sw, 8)
        lines.append("")
        lines.append(st.c("dim", f"  {'REQ'.ljust(rw)}  {'STATUS'.ljust(8)}  "
                                 f"{'STRENGTH'.ljust(sw)}  REQUIREMENT"))
        room = max(st.width - (2 + rw + 2 + 8 + 2 + sw + 2), 20)
        for r in rows:
            text = " ".join(str(r.get("text", "")).split())
            if len(text) > room:
                text = text[: room - len(st.ell)] + st.ell
            status = str(r.get("status", ""))
            cell = st.c(colors.get(status, "dim"), status.ljust(8)) if status else " " * 8
            lines.append(f"  {str(r.get('req', '')).ljust(rw)}  {cell}  "
                         f"{str(r.get('strength', '')).ljust(sw)}  {text}")
    for n in obj.get("notes", []) or []:
        lines.append(_wrap("  - ", "  - ", str(n), st.width))
    return "\n".join(lines)


def fmt_evidence_add(obj: dict, st: Style) -> str:
    result = obj.get("result", "")
    detail = ""
    if obj.get("value") is not None:
        detail = f"{obj['value']}{(' ' + str(obj['unit'])) if obj.get('unit') else ''}"
    if obj.get("criterion"):
        detail += f"{', ' if detail else ''}{obj['criterion']}"
    sym = st.sym("ok" if result == "pass" else "error")
    return f"{sym} Recorded {obj.get('req')} {obj.get('type')} {result}" + (
        f" ({detail})" if detail else "")


def fmt_status(obj: dict, st: Style) -> str:
    ph = obj.get("phase", {})
    cur = ph.get("current")
    head = "Synth status"
    if cur:
        head += (f" {st.dash} phase {cur['n']} of {ph.get('last')}: {cur['name']} "
                 f"({str(cur['status']).replace('_', ' ')})   "
                 f"[{ph.get('done_count', 0)} of {ph.get('total', 0)} done]")
    elif ph.get("total"):
        head += f" {st.dash} all {ph['total']} phases done"
    else:
        head += f" {st.dash} phase information unavailable"
    a = obj.get("audit", {})
    e, w = a.get("errors", 0), a.get("warnings", 0)  # status already excludes "not yet" items
    unused = obj.get("unused_pins", [])
    bits = [plural(e, "error") if e else "no errors"]
    bits.append(plural(w, "warning") if w else "no warnings")
    extra = (" (" + ", ".join(f"GPIO {g}" for g in a["strapping"]) + " strapping)") if a.get("strapping") else ""
    lines = [head,
             f"{st.sym('error' if e else 'ok')} Audit      {', '.join(bits)}{extra}"]
    rt = obj.get("realtime", {})
    rtf = rt.get("findings", [])
    lines.append(f"{st.sym('ok') if rt.get('clean') else st.sym('error')} Real-time  "
                 + ("RT checks clean" if rt.get("clean") else f"{plural(len(rtf), 'finding')}"))
    for f in rtf:
        lines.append(_wrap("    ", "    ", f"{f.get('rule')}: {f.get('message', '')}", st.width))
    ev = obj.get("evidence", {}).get("summary", {})
    fail, gaps = ev.get("fail", 0), ev.get("missing", 0) + ev.get("stale", 0)
    sev = "error" if fail else ("warning" if gaps else "ok")
    ev_line = (f"{st.sym(sev)} Evidence   {ev.get('pass', 0)} of {ev.get('total', 0)} pass, "
               f"{ev.get('missing', 0)} missing")
    if fail:
        ev_line += f", {fail} fail"
    if ev.get("stale"):
        ev_line += f", {ev['stale']} stale"
    xp = obj.get("evidence", {}).get("expected_phase")
    if gaps and not fail and xp is not None:
        ev_line += f" (expected before phase {xp})"
    lines.append(ev_line)
    for f in obj.get("status_findings", []):
        lines.append(_wrap("  ! ", "  ! ", f["message"], st.width))
    if unused:
        lines.append("Pins not configured yet:")
        for u in unused:
            p = u.get("phase")
            where = f"phase {p['n']} {p['name']}" + (
                f" ({str(p['status']).replace('_', ' ')})" if p["status"] != "not_started" else ""
            ) if p else "no phase mentions it"
            lines.append(f"  GPIO {str(u['gpio']).ljust(3)}{str(u['role']).ljust(14)} {where}")
    if obj.get("next_check"):
        lines.append(_wrap("This phase's check: ", "This phase's check: ", obj["next_check"], st.width))
    watch = obj.get("watch") or []
    if watch:
        lines.append("Watch for:")
        for w in watch[:5]:
            t = "  - " + str(w)
            lines.append(t if len(t) <= st.width else t[:st.width - len(st.ell)] + st.ell)
        if len(watch) > 5:
            lines.append(f"  +{len(watch) - 5} more")
    rel_rows = obj.get("evidence", {}).get("relevant", [])
    if rel_rows:
        def lab(r: dict) -> str:
            return f"{r['req']} ({r['tests'][0]})" if r.get("tests") else r["req"]
        lines.append(_wrap("Requirements this phase can evidence: ",
                           "Requirements this phase can evidence: ",
                           ", ".join(lab(r) for r in rel_rows), st.width))
    for n in obj.get("notes", []) or []:
        lines.append(_wrap("  - ", "  - ", str(n), st.width))
    return "\n".join(lines)


def fmt_try(obj: dict, st: Style) -> str:
    """What-if result: what the proposed pin change would introduce or resolve."""
    intro, res = obj.get("introduced", []), obj.get("resolved", [])
    errs = [f for f in intro if f.get("severity") == "error"]
    warns = [f for f in intro if f.get("severity") != "error"]
    if errs:
        head = f"{st.sym('error')} This change would break {plural(len(errs), 'rule')}"
    elif warns:
        head = f"{st.sym('warning')} Allowed, with {plural(len(warns), 'new warning')}"
    else:
        head = f"{st.sym('ok')} No new findings"
    lines = [head, f"  change: {obj.get('change', '')}"]
    for n in obj.get("applied", []):
        lines.append(f"  {n}")
    for title, items in (("Would introduce", errs + warns), ("Would resolve", res)):
        if items:
            lines += ["", title]
            lines += finding_lines(items, st)
    lines += ["", "Nothing was changed; edit spec.cnl (and the code) yourself if you want it."]
    return "\n".join(lines)
