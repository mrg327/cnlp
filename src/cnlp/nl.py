"""Natural language -> controlled English, with self-consistency and questions.

The model only rewrites; cnl.parse is the judge. Several samples are parsed,
the most common manifest wins, and anything the samples disagree on, anything
the model said it didn't know, and anything the rule checkers reject becomes a
plain-English question for the person.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cnlp import cnl, fsm, gbnf, llm, rules
from cnlp.manifest import Manifest, Pin

DEFAULTS = {"chip": "esp32", "module": "esp32-wroom-32"}
DEFAULT_BOARD = "devkit-v1"

SYSTEM_PROMPT = '''You rewrite a hardware request as controlled English for an ESP32. \
Output only the spec, then a final line END.

Statements (one per line, lowercase, each ends with a '.'):
  chip is esp32.   module is esp32-wroom-32.   board is devkit-v1.
  wifi is on|off.  bluetooth is on|off.  deep sleep is on|off.  jtag is on|off.
  console is on uart0|uart1|uart2|usb|nothing.
  gpio N is input|output|open-drain ["adc"|"dac"|"interrupt"|"pwm"] "role" PULL [wakes from deep sleep on low|high].
  gpio N is i2c0 sda|scl  /  spi2 mosi|miso|sclk|cs  /  uart1 tx|rx  PULL.
  gpio N is high|low at reset.
  PULL is: with internal pull-up | with internal pull-down | with external pull-up |
           with external pull-down | with off-board pull-up | with no pull
  machine NAME starts in STATE.
  state NAME: on EVENT go to STATE; after COUNT EVENT go to STATE; every N ms|s|min do ACTION.
  (a state with nothing to do is just: state NAME.)

Rules:
- Never guess. If the request does not say something you need (which pin, whether there is a
  resistor on the board, whether Wi-Fi is used, which module), do NOT invent a value. Write a
  line  # question: <plain-English question>  instead, and leave out the statement or the
  statement's unknown part is asked about. Only a statement you can write completely may appear.
- But if a pin's number, direction and pull are all stated or obvious, WRITE its statement; do not
  only ask questions. Never ask for a name, role or reset state: take the role from the request's
  own words (LED, relay, buzzer, button, pot). Obvious: LED/relay/buzzer/motor = output with no
  pull; button/switch = input; an analog sensor or pot = input adc with no pull; a UART line =
  uartN tx|rx with no pull.
- Outputs (LED, relay, buzzer...) are "with no pull" unless the request names a pull-up or pull-down
  on that output; a series resistor is not a pull.
- Pull on inputs: "internal pull-up" only if the request asks for the chip's pull. A resistor on
  the ESP32 board itself = external. A resistor on the breakout/module/sensor board = off-board
  pull-up (e.g. I2C breakout with its own pull-ups). "nothing on the board" = no pull, but a button
  to ground with nothing said about a resistor is a question, not a guess.
- "wakes the board from deep sleep" -> add "wakes from deep sleep on low|high" on that pin and
  "deep sleep is on.". "triggers an interrupt" -> the pin is "input interrupt".
- "int pullup" or "internal pullup" is the chip's internal pull-up; it is not an interrupt.
- A UART used for a peripheral (GPS, modem) is not the console; write a console line only if the
  request talks about serial debug/logging/console.
- Do not write chip/module/board lines unless the request names them.
- Before each statement write a line  # from: "<exact words from the request>".
- Machine, state and event names are lowercase words; events and actions may be several words.

Example request: "LED on 25. Button on 4 to ground, no resistor on the board, wifi off."
# from: "LED on 25"
gpio 25 is output "led" with no pull.
# from: "Button on 4 to ground, no resistor on the board"
gpio 4 is input "button" with no pull.
# from: "wifi off"
wifi is off.
END

Example request: "Pot on 34, I2C sensor breakout on 21/22 that has its own pull-ups, wifi off."
# from: "Pot on 34"
gpio 34 is input adc "pot" with no pull.
# from: "I2C sensor breakout on 21/22 that has its own pull-ups"
gpio 21 is i2c0 sda with off-board pull-up.
gpio 22 is i2c0 scl with off-board pull-up.
# from: "wifi off"
wifi is off.
END

Example request: "Something on pin 5."
# question: What is connected to GPIO 5, and is it an input or an output?
END

Example request: "Button on 4 with the internal pull-up. Press it to start a measurement every \
10 seconds; if 3 reads fail it goes to error, a button press resets it."
# from: "Button on 4 with the internal pull-up"
gpio 4 is input "button" with internal pull-up.
machine sensor starts in idle.
# from: "Press it to start a measurement"
state idle: on button press go to measuring.
# from: "every 10 seconds; if 3 reads fail it goes to error"
state measuring: every 10 s do read sensor; after 3 read failed go to error.
# from: "a button press resets it"
state error: on button press go to idle.
END'''


@dataclass
class TranslateResult:
    request: str = ""
    spec_text: str = ""  # raw text of the winning sample, comments kept
    canonical: str = ""  # cnl.render of the winning manifest ("" if nothing parsed)
    agreed: bool = False
    disagreements: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    assumptions: list[dict] = field(default_factory=list)
    sources: dict[int, str] = field(default_factory=dict)
    parse_failures: int = 0
    samples: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)  # parse errors if no sample parsed
    manifest: Manifest | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.manifest is not None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("manifest")
        d["sources"] = {str(k): v for k, v in self.sources.items()}
        d["ok"] = self.ok
        return d


_STATE_LINE = re.compile(r"^state ([a-z][a-z0-9_]*):\s*(.*?)\.\s*$")


_BARE_STATE = re.compile(r"^state ([a-z][a-z0-9_]*)\.\s*$")


def _merge_state_lines(text: str) -> str:
    """Join repeated `state X: ...` lines of one machine into the first one.

    The model often splits a state across lines; the language itself stays strict
    (FSM_DUPLICATE) for people. Deterministic: transitions keep their order.
    """
    out: list[str] = []
    seen: dict[str, int] = {}  # state name -> index in out, reset per machine
    for line in text.splitlines():
        if line.startswith("machine "):
            seen = {}
        bare = _BARE_STATE.match(line.strip())
        if bare:
            if bare.group(1) in seen:  # already declared: a bare repeat adds nothing
                continue
            seen[bare.group(1)] = len(out)
            out.append(line)
            continue
        m = _STATE_LINE.match(line.strip())
        if m and m.group(1) in seen:
            i = seen[m.group(1)]
            if _BARE_STATE.match(out[i].strip()):  # earlier bare `state X.` gets its transitions
                out[i] = line
            else:
                out[i] = out[i].rstrip()[:-1] + "; " + m.group(2) + "."
            continue
        if m:
            seen[m.group(1)] = len(out)
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


_PIN_LINE = re.compile(
    r'^gpio (?P<n>\d+) is (?P<mode>input|output|open-drain|(?:i2c|spi|uart)\d [a-z]+)(?P<func> (?:adc|dac|interrupt|pwm))?'
    r'(?P<role> "[^"]*")?(?P<pull> with (?:(?:internal|external|off-board) pull-(?:up|down)|no pull))?'
    r'(?P<wake> wakes from deep sleep on (?:low|high))?\.$')
_OFFBOARD = re.compile(r"\b(?:breakout|module|sensor|peripheral|daughter|add-?on)\s+board\b"
                       r"|\boff[- ]?board\b|\bon the (?:breakout|sensor|module)\b", re.IGNORECASE)
_PULL_WORD = re.compile(r"\bpull(?:ed)?[ -]?(?:up|down)s?\b|\bpullups?\b|\bpulldowns?\b", re.IGNORECASE)
_INTERRUPT_WORD = re.compile(r"\binterrupts?\b|\bisr\b|\bedge\b|\btriggers?\b|\bwake", re.IGNORECASE)
_CLAUSE_SPLIT = re.compile(r"[.;!?\n,]")


def _clauses_about(request: str, gpio: str, *, whole_sentence: bool = False) -> str:
    """Request text near a pin: the clauses (or sentences) that name its number."""
    splitter = re.compile(r"[.;!?\n]") if whole_sentence else _CLAUSE_SPLIT
    return " ".join(c for c in splitter.split(request) if re.search(rf"(?<!\d){gpio}(?!\d)", c))


def _normalize(text: str, request: str) -> tuple[str, list[dict]]:
    """Unambiguous, deterministic fixes to a sample before it is parsed or voted on.

    Every change is returned as a note so the person can see it. Anything
    ambiguous is left alone (the checkers or the vote will turn it into a question).
    """
    out: list[str] = []
    notes: list[dict] = []
    quote = ""
    wants_sleep = False
    for line in text.splitlines():
        s = line.strip()
        qm = re.match(r'#\s*from:\s*"?(.*?)"?\s*$', s)
        if qm:
            quote = qm.group(1)
        m = _PIN_LINE.match(s)
        if m:
            n, pull, func = m["n"], m["pull"], m["func"]
            near = quote + " " + _clauses_about(request, n)
            sentences = quote + " " + _clauses_about(request, n, whole_sentence=True)
            if m["mode"] == "output" and pull and pull != " with no pull" \
                    and not _PULL_WORD.search(near):
                notes.append({"field": f"gpio {n} pull", "value": "no pull", "source": "normalized",
                              "question": f"GPIO {n} is an output; dropped the pull the request never mentioned."})
                pull = " with no pull"
            elif pull and re.search(r"external pull-(?:up|down)", pull) and m["mode"] != "output" \
                    and (_OFFBOARD.search(sentences) or (m["mode"].startswith("i2c") and
                                                          _OFFBOARD.search(request))):
                pull = pull.replace("external", "off-board")
                notes.append({"field": f"gpio {n} pull", "value": "off-board pull-up",
                              "source": "normalized",
                              "question": f"The request puts the resistor on the breakout/module board, "
                                          f"so GPIO {n} is off-board, not external."})
            if m["mode"] == "input" and func is None and re.search(r"\binterrupts?\b", near, re.IGNORECASE):
                func = " interrupt"
                notes.append({"field": f"gpio {n} function", "value": "interrupt",
                              "source": "normalized",
                              "question": f"The request says GPIO {n} triggers an interrupt."})
            if func == " interrupt" and not _INTERRUPT_WORD.search(near):
                func = None
                notes.append({"field": f"gpio {n} function", "value": "gpio", "source": "normalized",
                              "question": f"The request never says GPIO {n} triggers an interrupt; "
                                          "dropped it."})
            wants_sleep = wants_sleep or bool(m["wake"])
            line = (f"gpio {n} is {m['mode']}{func or ''}{m['role'] or ''}{pull or ''}"
                    f"{m['wake'] or ''}.")
        if _PIN_LINE.match(s) and line in out:  # the model repeated a pin statement verbatim
            continue
        out.append(line)
    res = "\n".join(out) + ("\n" if text.endswith("\n") else "")
    if wants_sleep and not re.search(r"(?m)^deep sleep is\b", res):
        res = "deep sleep is on.\n" + res
        notes.append({"field": "features.deep_sleep", "value": True, "source": "normalized",
                      "question": "A pin wakes from deep sleep, so deep sleep is on."})
    return res, notes


def _pin_numbers_in(request: str) -> set[int]:
    """GPIO numbers the request names plainly (gpio 4, IO4, pin 4, D4, 'on 25')."""
    nums = set()
    for m in re.finditer(r"(?<![a-z0-9])(?:gpios?|io|pins?|d)\s*#?(\d{1,2})(?!\d)"
                         r"(?:\s*(?:,|and|&|/)\s*#?(\d{1,2})(?!\d))*", request, re.IGNORECASE):
        nums.update(int(x) for x in re.findall(r"\d+", m.group(0)))
    for m in re.finditer(r"\bon (\d{1,2})\b(?!\s*(?:k|v|s|ms|hz|min|sec|%|x))", request, re.IGNORECASE):
        nums.add(int(m.group(1)))
    return {n for n in nums if n <= 39}


def _missing_pins(text: str, request: str) -> list[int]:
    stated = {int(x) for x in re.findall(r"(?m)^gpio (\d+) is\b", text)}
    return sorted(_pin_numbers_in(request) - stated)


def _strip_end(text: str) -> str:
    t = text.rstrip()
    t = re.sub(r"\n?\s*END\s*$", "", t) if t.endswith("END") else t
    return t.strip() + "\n"


def _ensure_defaults(text: str, board: str) -> tuple[str, list[dict]]:
    """Insert chip/module/board lines the model omitted; return (text, assumptions)."""
    want = {"chip": DEFAULTS["chip"], "module": DEFAULTS["module"], "board": board}
    asked = {"chip": "Is the chip an ESP32?", "module": "Which module is on the board?",
             "board": "Which board is it?"}
    add, notes = [], []
    for key, value in want.items():
        if not re.search(rf"(?m)^{key} is\b", text):
            add.append(f"{key} is {value}.")
            notes.append({"field": key, "value": value, "question": asked[key],
                          "source": "default"})
    if add:
        add.insert(0, "# assumed defaults (not in the request); confirm at the intent gate")
    return ("\n".join(add) + "\n" + text if add else text), notes


def _questions_in(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"(?m)^\s*#\s*question:\s*(.+?)\s*$", text)]


def _sources_in(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    quote = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'#\s*from:\s*"?(.*?)"?\s*$', s)
        if m:
            quote = m.group(1)
        elif s.startswith("#") or not s:
            continue
        else:
            pm = re.match(r"gpio (\d+) is (?!(?:high|low) at reset)", s)
            if pm and quote:
                out.setdefault(int(pm.group(1)), quote)
            quote = None
    return out


_PULL_WORDS = {
    "none": "no pull",
    "internal_up": "internal pull-up",
    "internal_down": "internal pull-down",
    "external_up": "external pull-up",
    "external_down": "external pull-down",
    "external_offboard": "off-board pull-up",
}


def _val(field_name: str, v: Any) -> str:
    if v is None:
        return "none"
    if field_name == "pull":
        return _PULL_WORDS.get(v, str(v))
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v).replace("_", " ")


def _or(items: list[str]) -> str:
    return " vs ".join(items)


def _disagreements(manifests: list[Manifest]) -> list[dict]:
    out: list[dict] = []
    if not manifests:
        return out

    def distinct(vals: list) -> list:
        seen: list = []
        for v in vals:
            if v not in seen:
                seen.append(v)
        return seen

    gpios = sorted({p.gpio for m in manifests for p in m.pins})
    for g in gpios:
        pins: list[Pin | None] = [next((p for p in m.pins if p.gpio == g), None) for m in manifests]
        if len({p is None for p in pins}) > 1:
            out.append({"kind": "pin", "gpio": g, "field": "present",
                        "values": ["used" if p else "unused" for p in distinct(pins)],
                        "question": f"Runs disagreed on whether GPIO {g} is used at all. "
                                    f"Is something connected to GPIO {g}?"})
            continue
        for f in ("mode", "pull", "role", "function", "wake", "reset_level"):
            vals = distinct([getattr(p, f) for p in pins if p])
            if len(vals) > 1:
                names = [_val(f, v) for v in vals]
                out.append({"kind": "pin", "gpio": g, "field": f, "values": names,
                            "question": f"Runs disagreed on GPIO {g}'s {f.replace('_', ' ')}: "
                                        f"{_or(names)}. Which is it?"})
    for f in ("wifi", "bluetooth", "deep_sleep", "jtag", "console"):
        vals = distinct([getattr(m.features, f) for m in manifests])
        if len(vals) > 1:
            names = [_val(f, v) for v in vals]
            label = f.replace("_", " ")
            out.append({"kind": "feature", "field": f, "values": names,
                        "question": f"Runs disagreed on {label}: {_or(names)}. Which is it?"})
    mnames = sorted({sm.name for m in manifests for sm in m.state_machines})
    for n in mnames:
        sms = [next((sm for sm in m.state_machines if sm.name == n), None) for m in manifests]
        if len({sm is None for sm in sms}) > 1:
            out.append({"kind": "machine", "name": n, "field": "present", "values": [],
                        "question": f"Runs disagreed on whether there is a '{n}' state machine. "
                                    "Should there be one?"})
        elif len(distinct(sms)) > 1:
            out.append({"kind": "machine", "name": n, "field": "definition", "values": [],
                        "question": f"Runs disagreed on how the '{n}' state machine behaves. "
                                    "Can you describe its states and what moves between them?"})
    return out


NUDGE = ("The request names GPIO {pins} but your spec has no statement for {them}. Write a "
         "statement for every pin whose direction is stated or obvious from the request (LED, "
         "relay, buzzer = output with no pull; analog sensor or pot = input adc with no pull; "
         "UART pins = uartN tx|rx with no pull). Never ask for a name or role. Ask a question only "
         "about what is truly missing. Output the full spec, ending with END.")


def _prep(raw: str, request: str) -> tuple[str, list[dict]]:
    return _normalize(_merge_state_lines(_strip_end(raw)), request)


def _one_sample(client, messages: list[dict], seed: int, board: str, request: str = ""):
    """Return (raw_text, manifest|None, errors, assumptions, full_text)."""
    raw = client.complete(messages, grammar=gbnf.grammar(), temperature=0.7,
                          max_tokens=1500, seed=seed)
    text, norm_notes = _prep(raw, request)
    # A sample that asked questions yet skipped pins the request plainly names gets one nudge.
    missing = _missing_pins(text, request)
    if missing and _questions_in(text):
        nudge = NUDGE.format(pins=", ".join(map(str, missing)),
                             them="it" if len(missing) == 1 else "them")
        raw2 = client.complete(
            messages + [{"role": "assistant", "content": raw}, {"role": "user", "content": nudge}],
            grammar=gbnf.grammar(), temperature=0.7, max_tokens=1500, seed=seed + 200)
        text2, notes2 = _prep(raw2, request)
        if len(_missing_pins(text2, request)) < len(missing):
            raw, text, norm_notes = raw2, text2, notes2
    for attempt in (0, 1):
        full, notes = _ensure_defaults(text, board)
        try:
            return text, cnl.parse(full), [], notes + norm_notes, full
        except cnl.CnlError as e:
            errors = e.to_list()
            if attempt:
                return text, None, errors, notes + norm_notes, full
            msg = ("Your output had these errors:\n"
                   + "\n".join(f"- {x['message']}" for x in errors)
                   + "\nOutput the full corrected spec, ending with END.")
            raw = client.complete(
                messages + [{"role": "assistant", "content": raw},
                            {"role": "user", "content": msg}],
                grammar=gbnf.grammar(), temperature=0.7, max_tokens=1500, seed=seed + 100)
            text, norm_notes = _prep(raw, request)
    raise AssertionError("unreachable")


def translate(request: str, *, n: int = 3, client=llm, board: str = DEFAULT_BOARD) -> TranslateResult:
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": request}]
    res = TranslateResult(request=request)
    good: list[tuple[str, Manifest, list[dict], str]] = []  # (raw, manifest, notes, full)
    last_errors: list[dict] = []
    for i in range(n):
        text, m, errors, notes, full = _one_sample(client, messages, seed=1000 + i, board=board,
                                                    request=request)
        res.samples.append(text)
        if m is None:
            res.parse_failures += 1
            last_errors = errors
        else:
            good.append((text, m, notes, full))
    if not good:
        res.errors = last_errors
        res.spec_text = res.samples[0] if res.samples else ""
        res.questions = _dedupe(
            [q for s in res.samples for q in _questions_in(s)]
            + ["I couldn't turn that into a valid spec: " + e["message"] for e in last_errors])
        return res

    groups: list[list[int]] = []
    for i, (_, m, _, _) in enumerate(good):
        for g in groups:
            if good[g[0]][1] == m:
                g.append(i)
                break
        else:
            groups.append([i])
    win = max(groups, key=len)  # max returns the first of equals
    raw, m, notes, full = good[win[0]]
    res.manifest = m
    res.spec_text = full  # winning sample plus any inserted defaults, so it parses as written
    res.canonical = cnl.render(m)
    res.agreed = len(groups) == 1 and res.parse_failures == 0
    res.disagreements = _disagreements([g[1] for g in good])
    res.assumptions = cnl.assumptions(m, full) + notes
    res.sources = _sources_in(raw)
    rule_q = [f.message for f in rules.check(m) + fsm.check(m) if f.severity == "error"]
    res.questions = _dedupe([q for s in res.samples for q in _questions_in(s)]
                            + [d["question"] for d in res.disagreements] + rule_q)
    return res


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for q in items:
        k = q.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(q.strip())
    return out


def write_project(project: Path, request: str, result: TranslateResult) -> None:
    project = Path(project)
    (project / ".cnlp").mkdir(parents=True, exist_ok=True)
    (project / "request.md").write_text(request if request.endswith("\n") else request + "\n")
    (project / "spec.cnl").write_text(result.spec_text)
    (project / ".cnlp" / "translation.json").write_text(
        json.dumps(result.to_dict(), indent=2) + "\n")
