"""Staged state-machine translation.

Stage 1 (model, JSON-schema constrained): name the machines, states, events and
actions the request describes. Verified deterministically.
Stage 2 (model, grammar constrained): write the machine/state lines using only
those names (gbnf.fsm_grammar), so a state, target, event or action cannot be
invented. The result is checked by fsm.check and the plan's own coverage rules.
"""

from __future__ import annotations

import json
import re

from cnlp import cnl, fsm, gbnf

MAX_MACHINES, MAX_STATES, MAX_EVENTS, MAX_ACTIONS = 3, 12, 16, 8

# Wording that says "behaviour over time": modes, states, retries, timeouts, sequences.
_STATEFUL = re.compile(
    r"\b(?:state|states|mode|modes|retry|retries|time[- ]?outs?|cycling|cycles? (?:the|through|between)|"
    r"press(?:ed|ing)? again|stay(?:s)? in|go(?:es)? back|"
    r"goes? to (?!ground|gpio|uart|the |vcc|gnd|pin|io\d|\d)\w+|"
    r"fails? \d+|\d+ (?:times|tries|attempts|reads? fail\w*)|fails? (?:twice|thrice)|"
    r"then (?:repeat|go|goes|back|it)|repeat(?:s|ing)?|arms?|armed|disarm\w*|"
    r"(?:after|for) \d+(?:\.\d+)? ?(?:ms|s|sec\w*|min\w*)\b|"
    r"when [^.]{0,60}\b(?:then|goes?|go)\b|if [^.]{0,60}\b(?:goes?|go to|fails?)\b|"
    r"starts? (?:in|off|disarmed|closed|idle|open)|locked?|blocked)\b",
    re.IGNORECASE)

SCHEMA = {
    "type": "object",
    "properties": {"machines": {"type": "array", "maxItems": MAX_MACHINES, "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "states": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_STATES},
            "start": {"type": "string"},
            "events": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_EVENTS},
            "actions": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ACTIONS},
            "timers": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_STATES},
        },
        "required": ["name", "states", "start", "events", "actions", "timers"],
    }}},
    "required": ["machines"],
}

PLAN_PROMPT = '''You read a hardware request and list the state machines it describes. Output JSON only:
{"machines": [{"name", "states", "start", "events", "actions", "timers"}]}
- name: one short snake_case noun for what the machine controls (e.g. sensor, lamp, door).
- states: every mode the request names or implies (idle, measuring, error, off, on ...), snake_case,
  one word where possible. Do not invent states the request does not describe.
- start: the state it begins in.
- events: things that happen and cause a change: button_press, read_ok, read_failed, timeout,
  overheat ... snake_case, 1-3 words. Use the request's own words.
- actions: things done periodically while in a state ("read the sensor every 10 seconds" ->
  read_sensor). Empty list if none.
- timers: the states that end by themselves after a stated length of time ("after 30 s it stops",
  "red for 20 s", "times out after 5 minutes"). Empty list if none. A duration is a timer, not an
  event: do not also add a "timeout" event for it. A count ("3 failed reads") is an event, not a timer.
If the request describes no behaviour over time, output {"machines": []}.'''

WRITE_PROMPT = '''You write the state machine part of a controlled-English spec. Output only the lines, \
then a final line END.
Lines (lowercase, each ends with a '.'):
  machine NAME starts in STATE.
  state NAME: on EVENT go to STATE; after COUNT EVENT go to STATE; after N ms|s|min go to STATE;
              every N ms|s|min do ACTION.
Write one `state` line for every listed state, in order. Use only the listed events and actions.
`on EVENT go to STATE` = when the event happens. `after COUNT EVENT go to STATE` = after COUNT of
the same event in a row (e.g. 3 failed reads). `every N s do ACTION` = periodic work while in the
state. Every state needs a way out (except a final one), and every listed event and action
should be used where the request says so.
Timers versus counts: `after 3 read failed go to error` counts events ("after 3 failures"). \
`after 20 s go to idle` is a timer: 20 s after entering the state with nothing else happening, go to
idle ("after 20 s", "times out after 20 s", "red for 20 s then green"). A unit (ms, s, min) after the
number always means a timer. Only states in the plan's timers list get a timer, and only when the
request states the duration; use the request's own number and unit. Several transitions in one state go on one line,
separated by "; ".

Example plan: sensor: states idle, measuring, error; start idle; events button_press, read_ok,
read_failed; actions read_sensor. Request: "Press the button to measure; every 10 s read the
sensor; a good read goes back to idle; 3 failed reads is an error; a press clears the error."
machine sensor starts in idle.
state idle: on button press go to measuring.
state measuring: every 10 s do read sensor; on read ok go to idle; after 3 read failed go to error.
state error: on button press go to idle.
END'''


def looks_stateful(request: str) -> bool:
    return bool(_STATEFUL.search(request))


def _snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def validate_plan(data) -> tuple[list[dict], list[str]]:
    """Normalize a stage-1 reply; return (machines, errors). Errors mean 'retry'."""
    errors: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("machines"), list):
        return [], ['the reply must be an object with a "machines" list']
    out: list[dict] = []
    names: set[str] = set()
    for raw in data["machines"][:MAX_MACHINES]:
        if not isinstance(raw, dict):
            errors.append("each machine must be an object")
            continue
        name = _snake(raw.get("name", ""))
        states = list(dict.fromkeys(_snake(s) for s in raw.get("states") or [] if _snake(s)))
        start = _snake(raw.get("start", ""))
        events = list(dict.fromkeys(_snake(e) for e in raw.get("events") or [] if _snake(e)))
        actions = list(dict.fromkeys(_snake(a) for a in raw.get("actions") or [] if _snake(a)))
        timers = list(dict.fromkeys(_snake(t) for t in raw.get("timers") or [] if _snake(t)))
        label = name or "?"
        if (err := cnl._fsm_name(name, "machine")) is not None:
            errors.append(f"machine {label}: {err}")
        elif name in names:
            errors.append(f"machine name '{name}' is used twice; names must be unique")
        names.add(name)
        if len(states) < 2:
            errors.append(f"machine {label}: list at least two states")
        for s in states:
            if (err := cnl._fsm_name(s, "state")) is not None:
                errors.append(f"machine {label}: {err}")
        if start not in states:
            errors.append(f"machine {label}: start '{start}' must be one of the states")
        for t in timers:
            if t not in states:
                errors.append(f"machine {label}: timer state '{t}' must be one of the states")
        for kind, items in (("event", events), ("action", actions)):
            for it in items:
                _, err = cnl._fsm_phrase(it.split("_"), kind)
                if err:
                    errors.append(f"machine {label}: {err}")
        out.append({"name": name, "states": states, "start": start,
                    "events": events[:MAX_EVENTS], "actions": actions[:MAX_ACTIONS],
                    "timers": [t for t in timers if t in states]})
    return out, errors


def plan(client, request: str, seed: int = 500) -> list[dict] | None:
    """Stage 1. Returns verified machines, [] if the request has none, None on failure."""
    messages = [{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": request}]
    for attempt in (0, 1):
        try:
            raw = client.complete(messages, json_schema=SCHEMA, temperature=0.2,
                                  max_tokens=900, seed=seed + attempt)
            data = json.loads(raw)
        except (ValueError, TypeError):
            raw, data = "", None
        machines, errors = validate_plan(data) if data is not None else ([], ["reply was not JSON"])
        if not errors:
            return machines
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "Fix these problems and output the full JSON again:\n"
             + "\n".join(f"- {e}" for e in errors)}]
    return None


def outline(machines: list[dict]) -> str:
    return "\n".join(
        f"- machine {m['name']}: states {', '.join(m['states'])}; start {m['start']}; "
        f"events {', '.join(e.replace('_', ' ') for e in m['events']) or 'none'}; "
        f"actions {', '.join(a.replace('_', ' ') for a in m['actions']) or 'none'}; "
        f"timers {', '.join(m.get('timers') or []) or 'none'}"
        for m in machines)


def write_messages(machines: list[dict], request: str) -> list[dict]:
    return [{"role": "system", "content": WRITE_PROMPT},
            {"role": "user", "content": f"Plan:\n{outline(machines)}\n\nRequest: {request}"}]


_WORD_NUMBERS = {"once": 1, "one": 1, "twice": 2, "two": 2, "thrice": 3, "three": 3, "four": 4,
                 "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def request_numbers(request: str) -> set[int]:
    """Counts and periods the request states (digits or number words), 1-99."""
    nums = {int(x) for x in re.findall(r"(?<![\d.])(\d{1,2})(?![\d])", request)}
    for w in re.findall(r"[a-z]+", request.lower()):
        if w in _WORD_NUMBERS:
            nums.add(_WORD_NUMBERS[w])
    return {n for n in nums if 1 <= n <= 99}


def request_units(request: str) -> set[str]:
    """Time units the request uses; empty means it names none (then every unit is allowed)."""
    units = set()
    if re.search(r"\d\s*(?:ms|milliseconds?)\b|\bmilliseconds?\b", request, re.IGNORECASE):
        units.add("ms")
    if re.search(r"\d\s*(?:s|secs?|seconds?)\b|\bseconds?\b", request, re.IGNORECASE):
        units.add("s")
    if re.search(r"\d\s*(?:min|mins|minutes?)\b|\bminutes?\b", request, re.IGNORECASE):
        units.add("min")
    return units


def write(client, messages: list[dict], machines: list[dict], seed: int,
          numbers: set[int] | None = None, units: set[str] | None = None) -> str:
    return client.complete(messages, grammar=gbnf.fsm_grammar(machines, numbers, units),
                           temperature=0.7,
                           max_tokens=1200, seed=seed)


_DURATION = re.compile(
    r"(?<![\w.])(\d{1,5})\s*(ms|milliseconds?|s|secs?|seconds?|min|mins|minutes?)\b(?!\s*-)",
    re.IGNORECASE)


def request_durations(request: str) -> set[tuple[int, str]]:
    """(number, unit) pairs the request states as a length of time, skipping periods
    introduced by 'every'/'each' (those are periodic actions, not timers)."""
    out = set()
    for m in _DURATION.finditer(request):
        if re.search(r"\b(?:every|each)\s*$", request[:m.start()], re.IGNORECASE):
            continue
        u = m.group(2).lower()
        out.add((int(m.group(1)), "min" if u.startswith("min") else "ms" if u.startswith("m") else "s"))
    return out


def timeouts_from_events(text: str, request: str) -> str:
    """Turn a faked 'on timeout go to X' into 'after N s go to X' when the request states
    exactly one length of time (not one introduced by 'every'). Anything less clear is left
    alone: guessing which duration belongs to which state would invent a requirement."""
    durations = request_durations(request)
    if len(durations) != 1:
        return text
    (n, unit), = durations
    out = []
    for line in text.splitlines():
        m = re.match(r"^(state [a-z][a-z0-9_]*): (.*)\.$", line.strip())
        if m:
            parts = m.group(2).split("; ")
            if (sum(p.startswith("on timeout go to ") for p in parts) == 1
                    and not any(re.match(r"after \d+ (?:ms|s|min) go to ", p) for p in parts)):
                parts = [f"after {n} {unit} go to {p[len('on timeout go to '):]}"
                         if p.startswith("on timeout go to ") else p for p in parts]
                line = f"{m.group(1)}: {'; '.join(parts)}."
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def finish(text: str, request: str) -> str:
    """Post-process written machine lines: timers from faked timeouts, then a stable order."""
    return sort_transitions(timeouts_from_events(text, request))


def sort_transitions(text: str) -> str:
    """Order each state's transitions alphabetically: order carries no meaning, but it
    would make otherwise identical samples look different when voting."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(state [a-z][a-z0-9_]*): (.*)\.$", line.strip())
        out.append(f"{m.group(1)}: {'; '.join(sorted(m.group(2).split('; ')))}." if m else line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def problems(manifest, machines: list[dict]) -> list[str]:
    """Findings worth a targeted retry: FSM errors, dead ends, unused planned names."""
    out = [f.message for f in fsm.check(manifest)
           if f.severity == "error" or f.rule == "FSM_DEAD_END"]
    for plan_m in machines:
        sm = next((s for s in manifest.state_machines if s.name == plan_m["name"]), None)
        if sm is None:
            out.append(f"machine '{plan_m['name']}' is missing.")
            continue
        events = {t.event for st in sm.states for t in st.transitions if t.kind in ("on", "after")}
        if any(t.kind == "timeout" for st in sm.states for t in st.transitions):
            events.add("timeout")  # a planned 'timeout' event is covered by a real timer
        actions = {t.action for st in sm.states for t in st.transitions if t.kind == "every"}
        for e in plan_m["events"]:
            if e not in events:
                out.append(f"In machine '{sm.name}', the event '{e.replace('_', ' ')}' is never "
                           "used; use it where the request says it matters.")
        for tname in plan_m.get("timers") or []:
            st = next((s for s in sm.states if s.name == tname), None)
            if st is not None and not any(t.kind == "timeout" for t in st.transitions):
                out.append(f"In machine '{sm.name}', state '{tname}' should leave by itself after the "
                           "time the request states; add 'after N s go to STATE' to it.")
        for a in plan_m["actions"]:
            if a not in actions:
                out.append(f"In machine '{sm.name}', the action '{a.replace('_', ' ')}' is never "
                           "done; add an 'every N s do ...' in the right state.")
    return out
