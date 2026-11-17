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
- "nothing on the board" or "no resistor" means with no pull. A resistor on the board or
  breakout means external. Say internal pull only if the request asks for it.
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


def _one_sample(client, messages: list[dict], seed: int, board: str):
    """Return (raw_text, manifest|None, errors, assumptions_from_defaults)."""
    raw = client.complete(messages, grammar=gbnf.grammar(), temperature=0.7,
                          max_tokens=1500, seed=seed)
    text = _strip_end(raw)
    for attempt in (0, 1):
        full, notes = _ensure_defaults(text, board)
        try:
            return text, cnl.parse(full), [], notes, full
        except cnl.CnlError as e:
            errors = e.to_list()
            if attempt:
                return text, None, errors, notes, full
            msg = ("Your output had these errors:\n"
                   + "\n".join(f"- {x['message']}" for x in errors)
                   + "\nOutput the full corrected spec, ending with END.")
            raw = client.complete(
                messages + [{"role": "assistant", "content": raw},
                            {"role": "user", "content": msg}],
                grammar=gbnf.grammar(), temperature=0.7, max_tokens=1500, seed=seed + 100)
            text = _strip_end(raw)
    raise AssertionError("unreachable")


def translate(request: str, *, n: int = 3, client=llm, board: str = DEFAULT_BOARD) -> TranslateResult:
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": request}]
    res = TranslateResult(request=request)
    good: list[tuple[str, Manifest, list[dict], str]] = []  # (raw, manifest, notes, full)
    last_errors: list[dict] = []
    for i in range(n):
        text, m, errors, notes, full = _one_sample(client, messages, seed=1000 + i, board=board)
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
