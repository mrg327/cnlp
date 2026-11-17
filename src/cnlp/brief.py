"""Model-facing output (`--for-model`): a compact, deterministic block meant to be relayed.

Local models paraphrase rule IDs and re-derive facts from files, so everything the model should
say is computed here from the same dicts the commands emit as JSON:

    REPORT (relay this; keep the rule IDs):   findings, each line starting with its rule ID
    FACTS (authoritative; don't re-derive from files):   phase, checks, requirement counts
    NEXT (suggest, don't do):   steps derived from finding types by a small table (`next_steps`)

At most MAX_LINES lines; findings are truncated with "+N more (run cnlp audit for all)".
Pure functions of the result dict: no I/O, no model.
"""

from __future__ import annotations

import re
from typing import Any

MAX_LINES = 25
OK, ERR, WARN, NOTE = "✓", "✗", "!", "○"
NOT_YET = ("AUDIT_UNUSED", "PARAM_MISSING", "PARAM_TASK_MISSING")
_LOC = re.compile(r"^(\S+?:\d+(?::\d+)?):\s+(.*)$", re.DOTALL)
_TASK = re.compile(r"^task (\S+) is created with (priority|core) (.+?), expected (\S+) "
                   r"\(([^:)]+?)(?:: [^)]*)?\)\.?$", re.DOTALL)
_CONST = re.compile(r"^(\w[\w.]*) is (.+?), expected (.+?) \(")
_PINS = re.compile(r"(?:Free pins that pass every rule for (\S+?): ([^.]+)\.)")


def _flat(text: Any) -> str:
    return " ".join(str(text).split())


def _sym(sev: str) -> str:
    return ERR if sev == "error" else WARN


def _split(f: dict) -> tuple[str, str, str]:
    """(rule, location or '', message without the location prefix)."""
    msg = _flat(f.get("message", ""))
    m = _LOC.match(msg)
    loc, msg = (m.group(1), m.group(2)) if m else ("", msg)
    return str(f.get("rule", "")), loc, msg


def _rewrite(rule: str, msg: str) -> str:
    """Plainer wording for rules whose raw message is easy to garble."""
    if rule == "PARAM_TASK":
        m = _TASK.match(msg)
        if m:
            task, what, actual, exp, src = m.groups()
            verb = "is on core" if what == "core" else "has priority"
            return f"{task} {verb} {actual}; the plan says {what} {exp} ({src})."
    return msg


def finding_line(f: dict) -> str:
    rule, loc, msg = _split(f)
    where = f" {loc}" if loc else ""
    return f"- {_sym(f.get('severity', 'warning'))} {rule}{where} — {_rewrite(rule, msg)}"


def _ordered(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: 0 if f.get("severity") == "error" else 1)


def _not_yet_line(fs: list[dict]) -> str | None:
    """One line for every 'not yet' item (expected mid-project; not problems)."""
    if not fs:
        return None
    pins = [f for f in fs if f.get("rule") == "AUDIT_UNUSED"]
    tasks = [f for f in fs if f.get("rule") == "PARAM_TASK_MISSING"]
    params = [f for f in fs if f.get("rule") == "PARAM_MISSING"]
    bits = []
    if pins:
        g = ", ".join(str(f["gpio"]) for f in pins if "gpio" in f)
        bits.append(f"AUDIT_UNUSED ×{len(pins)}: planned pins not configured in code yet"
                    + (f" (GPIO {g})" if g else ""))
    if tasks:
        names = [m.group(1) for f in tasks if (m := re.match(r"Task (\S+) ", str(f.get("message"))))]
        bits.append(f"PARAM_TASK_MISSING ×{len(tasks)}: tasks not created yet ({', '.join(names)})")
    if params:
        bits.append(f"PARAM_MISSING ×{len(params)}: expected constants/fields not defined yet")
    return f"- {NOTE} " + "; ".join(bits) + ". Expected mid-project; not problems."


# ---- NEXT table -----------------------------------------------------------------------------

def _pins_hint(msg: str) -> str:
    m = _PINS.search(msg)
    if m:
        return f" Suggested free pins for {m.group(1)}: {m.group(2).strip()}."
    m = re.search(r"these work with a warning \(check with cnlp try\): ([^.]+)\.", msg)
    return f" Possible pins (with a warning): {m.group(1)}." if m else ""


def _next_for(f: dict) -> str | None:
    rule, loc, msg = _split(f)
    at = f" at {loc}" if loc else ""
    if rule == "PARAM_TASK":
        m = _TASK.match(msg)
        if m:
            return f"Fix: create {m.group(1)} with {m.group(2)} {m.group(4)}{at} (user reviews the change)."
        return f"Fix: change the task's priority/core{at} to the plan's value."
    if rule == "PARAM_CONSTANT":
        m = _CONST.match(msg)
        return (f"Fix: change {m.group(1)} to {m.group(3)}{at}." if m
                else f"Fix: change the constant{at} to the plan's value.")
    if rule == "PARAM_DRIVER_FIELD":
        m = _CONST.match(msg)
        return (f"Fix: set driver field {m.group(1)} to {m.group(3)}{at}." if m
                else f"Fix: set the driver field{at} to the plan's value.")
    if rule == "PARAM_SDKCONFIG":
        k = re.search(r"CONFIG_\w+", msg)
        return (f"Fix: set {k.group(0)} as the message states in sdkconfig.defaults." if k else
                "Fix: set the sdkconfig option as the message states in sdkconfig.defaults.")
    if rule == "PARAM_UNRESOLVED":
        return f"Check by hand: the value{at} can't be resolved statically."
    if rule.startswith("RT1"):
        return f"Fix: remove the blocking call from the audio path{at}, or hand it to a lower-priority task."
    if rule.startswith("RT2"):
        return f"Fix: use timeout 0 on the queue call{at} in the real-time task."
    if rule.startswith("RT3"):
        return f"Fix: give the shared variable{at} a single writer task; pass changes by queue."
    if rule.startswith("RT4"):
        return f"Fix: add a blocking call (queue wait or vTaskDelayUntil) to the task loop{at}."
    if rule.startswith("RT5"):
        return f"Fix: use float, not double{at} (f-suffixed literals, sinf/cosf)."
    if rule.startswith("RT6"):
        return f"Fix: reorder app_main{at} so the audio task is created last."
    if rule == "RT7_PERIOD_MISMATCH":
        return f"Fix: make the vTaskDelayUntil period{at} match the profile's period_ms."
    if rule.startswith("RT7"):
        return f"Fix: pace the loop{at} with vTaskDelayUntil and a TickType_t last-wake time."
    if rule == "AUDIT_PULL":
        return f"Fix: make the code's pull setting{at} match the plan (or change the plan; check with cnlp_try)."
    if rule == "AUDIT_MODE":
        return f"Fix: set the pin's mode{at} to what the plan says."
    if rule == "AUDIT_DRIVER":
        return f"Fix: map the driver signal{at} to a pin that supports it, or change the plan (cnlp_try)."
    if rule == "AUDIT_CONFLICT":
        return f"Fix: give the pin one owner{at}; remove or move the other configuration."
    if rule == "AUDIT_UNPLANNED":
        return "Decide: add the pin to the pin plan (spec.cnl) or remove it from the code."
    if rule in ("C6", "W4"):
        q = re.search(r"'(gpio \d+ is (?:low|high) at reset)'", msg)
        if rule == "W4" and q:
            return f"Decide: declare the reset level in spec.cnl ('{q.group(1)}'), or move the pin."
        return "Decide: the strapping pin needs hardware-risk approval from the user, or a move."
    if rule == "OWNER" or re.fullmatch(r"[CW]\d+", rule):
        return f"Check a move with cnlp_try before editing.{_pins_hint(msg)}"
    return None


def next_steps(findings: list[dict], limit: int = 4) -> list[str]:
    """Deterministic follow-ups from finding types; each distinct step once, errors first."""
    out: list[str] = []
    for f in _ordered([f for f in findings if f.get("rule") not in NOT_YET]):
        s = _next_for(f)
        if s and s not in out:
            out.append(s)
    out = out[:limit]
    if any(s.startswith("Fix:") for s in out):
        out.append("Re-run cnlp_audit after the change; a finding is resolved only when it no longer appears.")
    return out


# ---- assembly -------------------------------------------------------------------------------

def _assemble(report: list[str], facts: list[str], nxt: list[str], *, more: str,
              keep_last: int = 0, max_lines: int = MAX_LINES) -> str:
    """REPORT/FACTS/NEXT, at most `max_lines` lines. The last `keep_last` report lines are
    summaries that are never dropped; findings before them are truncated."""
    facts, nxt = facts[:8], nxt[:6]
    fixed = 1 + (1 + len(facts) if facts else 0) + (1 + len(nxt) if nxt else 0)
    room = max(max_lines - fixed, keep_last + 2)
    head, tail = (report[:-keep_last], report[-keep_last:]) if keep_last else (report, [])
    if len(head) + len(tail) > room:
        keep = max(room - len(tail) - 1, 1)
        head = [*head[:keep], f"- +{len(head) - keep} more ({more})"]
    lines = ["REPORT (relay this; keep the rule IDs):", *head, *tail]
    if facts:
        lines += ["FACTS (authoritative; don't re-derive from files):", *facts]
    if nxt:
        lines += ["NEXT (suggest, don't do):", *[f"- {x}" for x in nxt]]
    return "\n".join(lines)


def _rt_line(rt: list[dict]) -> str:
    return (f"- {OK} Real-time rules RT1–RT7: clean." if not rt else
            f"- {ERR} Real-time rules RT1–RT7: {len(rt)} finding(s) above.")


def _is_rt(f: dict) -> bool:
    return str(f.get("rule", "")).startswith("RT")


def _audit_report(fs: list[dict]) -> tuple[list[str], int, list[dict]]:
    """(report lines, number of trailing summary lines, actionable findings)."""
    act = [f for f in fs if f.get("rule") not in NOT_YET]
    nyet = [f for f in fs if f.get("rule") in NOT_YET]
    lines = [finding_line(f) for f in _ordered(act)]
    keep = 0
    if not act:
        lines.append(f"- {OK} Pin plan, code vs plan and parameters: no findings.")
        keep += 1
    lines.append(_rt_line([f for f in act if _is_rt(f)]))
    keep += 1
    ny = _not_yet_line(nyet)
    if ny:
        lines.append(ny)
        keep += 1
    return lines, keep, act


def _all_findings(obj: dict) -> list[dict]:
    return [f for k in ("plan_findings", "audit_findings", "lint_findings", "param_findings")
            for f in (obj.get(k) or [])]


def brief_audit(obj: dict) -> str:
    report, keep, act = _audit_report(_all_findings(obj))
    s = obj.get("summary", {})
    facts = [(f"- {s.get('planned_pins', 0)} planned pins, {s.get('pins_in_code', 0)} found in "
              f"code, {s.get('files_scanned', 0)} files scanned.")]
    for k, v in (s.get("derived") or {}).items():
        facts.append(f"- Derived: {k} = {v}.")
    for n in (obj.get("notes") or [])[:2]:
        facts.append(f"- Note: {_flat(n)}")
    return _assemble(report, facts, next_steps(act), more="run cnlp audit for all", keep_last=keep)


def brief_status(obj: dict) -> str:
    ph = obj.get("phase", {})
    a = obj.get("audit", {})
    audit_fs = obj.get("_audit_findings")  # attached by the CLI so rule IDs are available
    if audit_fs is not None:
        report, keep, act = _audit_report(audit_fs)
    else:  # counts only; no rule IDs available
        e, w = a.get("errors", 0), a.get("warnings", 0)
        line = f"- {ERR if e else (WARN if w else OK)} Audit: {e} errors, {w} warnings"
        if a.get("strapping"):
            line += " (strapping: " + ", ".join(f"GPIO {g}" for g in a["strapping"]) + ")"
        act = list((obj.get("realtime") or {}).get("findings", []))
        report = [finding_line(f) for f in _ordered(act)]
        report += [line + (". Run cnlp_audit for the rule IDs." if e or w else "."), _rt_line(act)]
        keep = 2
        if a.get("not_yet"):
            report.append(f"- {NOTE} {a['not_yet']} 'not yet' items (planned pins/tasks not in code "
                          "yet): expected mid-project; not problems.")
            keep += 1
    for f in obj.get("status_findings") or []:
        report.insert(0, finding_line(f))
    ev = (obj.get("evidence") or {}).get("summary", {})
    xp = (obj.get("evidence") or {}).get("expected_phase")
    gaps = ev.get("missing", 0) + ev.get("stale", 0)
    eline = (f"- {ERR if ev.get('fail') else (WARN if gaps else OK)} Evidence: "
             f"{ev.get('pass', 0)} of {ev.get('total', 0)} requirements pass, "
             f"{ev.get('missing', 0)} missing")
    if ev.get("fail"):
        eline += f", {ev['fail']} fail"
    if ev.get("stale"):
        eline += f", {ev['stale']} stale"
    if gaps and not ev.get("fail") and xp is not None:
        eline += f" (missing is expected before phase {xp})"
    report.append(eline + ".")
    keep += 1

    facts = []
    cur = ph.get("current")
    if cur:
        facts.append(f"- Phase {cur['n']} of {ph.get('last')}: {cur['name']} "
                     f"({str(cur['status']).replace('_', ' ')}); {ph.get('done_count', 0)} of "
                     f"{ph.get('total', 0)} phases done.")
    elif ph.get("total"):
        facts.append(f"- All {ph['total']} phases done.")
    else:
        facts.append("- Phase information unavailable.")
    if obj.get("next_check"):
        facts.append(f"- Check: {_flat(obj['next_check'])}")
    watch = obj.get("watch") or []
    facts += [f"- Watch for: {_flat(w)}" for w in watch[:4]]
    if len(watch) > 4:
        facts.append(f"- Watch for: +{len(watch) - 4} more in cnlp status.")
    rel = (obj.get("evidence") or {}).get("relevant") or []
    if rel:
        facts.append("- Requirements this phase can evidence: " + ", ".join(
            f"{r['req']} ({r['tests'][0]})" if r.get("tests") else r["req"] for r in rel) + ".")
    unused = obj.get("unused_pins") or []
    if unused:
        by: dict[str, list[str]] = {}
        for u in unused:
            p = u.get("phase")
            by.setdefault(f"phase {p['n']} {p['name']}" if p else "no phase", []).append(
                f"GPIO {u['gpio']} {u['role']}")
        facts.append("- Pins not configured yet: " + "; ".join(
            f"{', '.join(v)} ({k})" for k, v in by.items()) + ".")
    nxt = next_steps(act)
    if audit_fs is None and (a.get("errors") or a.get("warnings")):
        nxt.append("Run cnlp_audit for the rule IDs behind the audit counts.")
    nxt.append("Work to the current phase's check above before moving on.")
    return _assemble(report, facts, nxt, more="run cnlp audit for all", keep_last=keep)


def brief_evidence(obj: dict) -> str:
    s = obj.get("summary", {})
    rows = obj.get("rows") or []
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(str(r.get("status")), []).append(r)
    miss = [str(r["req"]) for r in by.get("missing", [])]
    report = [(f"- {WARN if miss else OK} {s.get('pass', 0)} of {s.get('total', 0)} requirements "
               f"pass; {s.get('fail', 0)} fail, {s.get('missing', 0)} missing, "
               f"{s.get('stale', 0)} stale, {s.get('accepted', 0)} accepted.")]
    for st in ("fail", "stale"):
        report += [f"- {ERR if st == 'fail' else WARN} {r['req']} {st} ({r.get('strength')}) — "
                   f"{_flat(r.get('text', ''))[:110]}" for r in by.get(st, [])]
    for st in ("pass", "accepted"):
        report += [f"- {OK} {r['req']} {st} ({r.get('strength')}) — {_flat(r.get('text', ''))[:90]}"
                   for r in by.get(st, [])]
    keep = 0
    if miss:
        report.append(f"- {NOTE} Missing: {', '.join(miss)}. Normal until the verification phase.")
        keep = 1
    facts = []
    strong = [f"{r['req']}={r['strength']}" for r in rows if r.get("strength") not in (None, "none")]
    if strong:
        facts.append("- Strongest evidence so far: " + ", ".join(strong) + ".")
    facts += [f"- Note: {_flat(n)}" for n in (obj.get("notes") or [])[:2]]
    nxt = []
    if s.get("fail"):
        nxt.append("Fix the failing requirement(s), then ask the user to re-measure.")
    if miss or s.get("stale"):
        nxt.append("Evidence is recorded by the user, never by you: "
                   "cnlp evidence add . <REQ> <type> pass|fail --criterion \"...\".")
    return _assemble(report, facts, nxt, more="run cnlp evidence for all", keep_last=keep)


def brief_try(obj: dict) -> str:
    intro, res = obj.get("introduced") or [], obj.get("resolved") or []
    errs = [f for f in intro if f.get("severity") == "error"]
    if errs:
        head = f"- {ERR} This change would break {len(errs)} rule(s)."
    elif intro:
        head = f"- {WARN} Allowed, with {len(intro)} new warning(s)."
    else:
        head = f"- {OK} No new findings."
    report = [head, *[finding_line(f) for f in _ordered(intro)]]
    report += [f"- {OK} {r} [would be resolved]" for r in
               (finding_line(f).split(" ", 2)[2] for f in res)]
    facts = [f"- Proposed: {_flat(obj.get('change', ''))}"]
    facts += [f"- {_flat(n)}" for n in obj.get("applied") or []]
    # Already a what-if: don't tell the model to run cnlp_try again; point at the free pins instead.
    nxt = [x.replace("Check a move with cnlp_try before editing.", "Offer one of these instead.")
           for x in next_steps(intro) if not x.startswith("Re-run")]
    nxt.append("Nothing was changed. If the user wants it, they edit spec.cnl and the code; "
               "then re-run cnlp_audit.")
    return _assemble(report, facts, nxt, more="run cnlp try for all")


def brief_ask(obj: dict) -> str:
    kind = obj.get("kind")
    if kind == "command":
        body = [f"- Command: {obj.get('command')}"]
        if obj.get("why"):
            body.append(f"- Why: {_flat(obj['why'])}")
        if obj.get("refused"):
            body.append("- Refused: this command records something in the user's name; show it to them.")
        elif not obj.get("read_only", True):
            body.append("- It changes state or touches hardware: the user runs it, not you.")
    elif kind == "answer":
        body = [f"- {_flat(obj.get('answer', ''))}"]
    else:
        body = ["- cnlp doesn't know (its sources don't cover that). Say so; don't guess. Try: cnlp --help"]
    if obj.get("sources"):
        body.append(f"- Sources: {', '.join(obj['sources'])}")
    if obj.get("unsupported"):
        body.append(f"- Unsupported (not in cnlp's sources; double-check): {', '.join(obj['unsupported'])}")
    return "\n".join(["ANSWER (relay this; keep rule IDs and command names exactly):", *body])


BRIEFS = {"audit": brief_audit, "status": brief_status, "evidence": brief_evidence,
          "try": brief_try}
