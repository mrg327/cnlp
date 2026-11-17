"""State-machine checker: structural problems in the transition tables.

``check(m)`` returns Findings (gpio is always None). Rule IDs:
FSM_UNKNOWN_STATE, FSM_DUPLICATE, FSM_NONDETERMINISTIC, FSM_UNREACHABLE (errors);
FSM_DEAD_END, FSM_UNHANDLED, FSM_TIMER_SHADOWED (warnings).
"""

from __future__ import annotations

from collections import Counter

from cnlp.manifest import Finding, Manifest, StateMachine

# A state with these names is allowed to have no way out.
_TERMINAL = {"done", "stopped", "final", "end", "ended", "halt", "halted", "off", "terminal", "finished"}


def _join(items: list[str]) -> str:
    items = [f"'{i}'" for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _human(ev: str) -> str:
    return ev.replace("_", " ")


def _dur(ms: int) -> str:
    for unit, k in (("min", 60000), ("s", 1000)):
        if ms % k == 0:
            return f"{ms // k} {unit}"
    return f"{ms} ms"


def _check_machine(sm: StateMachine) -> list[Finding]:
    out: list[Finding] = []

    def err(rule: str, msg: str) -> None:
        out.append(Finding(rule, "error", msg))

    def warn(rule: str, msg: str) -> None:
        out.append(Finding(rule, "warning", msg))

    names = [s.name for s in sm.states]
    for n, c in Counter(names).items():
        if c > 1:
            err("FSM_DUPLICATE", f"Machine '{sm.name}' declares state '{n}' {c} times; "
                "declare each state once and put all its transitions in one line.")
    declared = set(names)
    if sm.start not in declared:
        err("FSM_UNKNOWN_STATE", f"Machine '{sm.name}' starts in '{sm.start}', "
            "but no 'state " + sm.start + ":' line declares it.")

    edges: dict[str, set[str]] = {n: set() for n in declared}
    for st in sm.states:
        seen: dict[tuple[str, str], int] = {}
        for t in st.transitions:
            if t.kind == "every":
                key = ("every", t.action or "")
            elif t.kind == "timeout":
                key = ("timeout", "")
                if t.target not in declared:
                    err("FSM_UNKNOWN_STATE",
                        f"In machine '{sm.name}', state '{st.name}' times out to '{t.target}' "
                        f"after {_dur(t.period_ms or 0)}, but no state '{t.target}' is declared.")
                elif st.name in edges:
                    edges[st.name].add(t.target or "")
            else:
                key = (t.kind, t.event or "")
                if t.target not in declared:
                    err("FSM_UNKNOWN_STATE",
                        f"In machine '{sm.name}', state '{st.name}' goes to '{t.target}' "
                        f"on '{_human(t.event or '')}', but no state '{t.target}' is declared.")
                elif st.name in edges:
                    edges[st.name].add(t.target or "")
            seen[key] = seen.get(key, 0) + 1
        for (kind, what), c in seen.items():
            if c > 1:
                if kind == "timeout":
                    msg = (f"In machine '{sm.name}', state '{st.name}' has {c} timers "
                           "('after N s go to ...'); a state can have only one, so keep one.")
                elif kind == "every":
                    msg = (f"In machine '{sm.name}', state '{st.name}' runs "
                           f"'{_human(what)}' periodically {c} times; keep one.")
                else:
                    msg = (f"In machine '{sm.name}', state '{st.name}' handles "
                           f"'{_human(what)}' {c} times with '{kind}'; keep one.")
                err("FSM_DUPLICATE", msg)
        on_ev = {t.event for t in st.transitions if t.kind == "on"}
        for t in st.transitions:
            if t.kind == "after" and t.event in on_ev:
                err("FSM_NONDETERMINISTIC",
                    f"In machine '{sm.name}', state '{st.name}' has both 'on {_human(t.event or '')}' "
                    f"and 'after {t.count} {_human(t.event or '')}'; it is unclear which wins. "
                    "Keep only one of them.")

    # Duplicate machine names are checked in check(); reachability below.
    if sm.start in declared:
        reach = {sm.start}
        todo = [sm.start]
        while todo:
            for nxt in edges.get(todo.pop(), ()):
                if nxt not in reach:
                    reach.add(nxt)
                    todo.append(nxt)
        for n in dict.fromkeys(names):
            if n not in reach:
                err("FSM_UNREACHABLE",
                    f"In machine '{sm.name}', state '{n}' can never be entered: no path leads "
                    f"to it from the start state '{sm.start}'. Add a transition into it or remove it.")

    for st in sm.states:
        if (not any(t.kind in ("on", "after", "timeout") for t in st.transitions)
                and st.name not in _TERMINAL):
            warn("FSM_DEAD_END",
                 f"In machine '{sm.name}', state '{st.name}' has no way out: no 'on' or 'after' "
                 "transition leaves it. If that is intended, name it 'done' or 'stopped'.")
        timers = [t for t in st.transitions if t.kind == "timeout"]
        if timers:
            limit = min(t.period_ms or 0 for t in timers)
            for t in st.transitions:
                if t.kind == "every" and (t.period_ms or 0) >= limit:
                    warn("FSM_TIMER_SHADOWED",
                         f"In machine '{sm.name}', state '{st.name}' would do '{_human(t.action or '')}' "
                         f"every {_dur(t.period_ms or 0)}, but the state's timer leaves after "
                         f"{_dur(limit)}, so that action never gets to run. Make the action's period "
                         "shorter than the timer, or lengthen the timer.")

    handled: dict[str, set[str]] = {}
    for st in sm.states:
        for t in st.transitions:
            if t.kind in ("on", "after") and t.event:
                handled.setdefault(t.event, set()).add(st.name)
    for ev, where in handled.items():
        missing = [n for n in dict.fromkeys(names) if n not in where]
        if missing:
            warn("FSM_UNHANDLED",
                 f"In machine '{sm.name}', event '{_human(ev)}' is handled in "
                 f"{_join([n for n in dict.fromkeys(names) if n in where])} but not in "
                 f"{_join(missing)}, where it is ignored.")
    return out


def check(m: Manifest) -> list[Finding]:
    out: list[Finding] = []
    for n, c in Counter(sm.name for sm in m.state_machines).items():
        if c > 1:
            out.append(Finding("FSM_DUPLICATE", "error",
                               f"The spec declares machine '{n}' {c} times; give each machine its own name."))
    for sm in m.state_machines:
        out.extend(_check_machine(sm))
    return out
