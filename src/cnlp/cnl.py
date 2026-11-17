"""Controlled English: parse a spec into a Manifest, render a Manifest back.

Decisions worth knowing:
- Keywords are lowercase only; errors say so.
- Parsed pins have ``source = None``. Traceability to the natural-language
  request is a later phase, and a fixed value keeps parse(render(m)) == m.
- Pins keep declaration order (the blinky fixture is in declaration order, and
  Manifest equality is order-sensitive). render() emits pins in manifest order.
  'off-board pull-up' and 'off-board pull-down' both mean external_offboard.
- Each statement is parsed on its own by a Lark LALR grammar, so errors carry
  the real line and column and several errors can be reported at once.
"""

from __future__ import annotations

import re

from lark import Lark, Token, Tree
from lark.exceptions import UnexpectedCharacters, UnexpectedInput, UnexpectedToken

from cnlp.manifest import Features, Manifest, Pin, State, StateMachine, Transition

IDF_VERSION = "5.5.6"

_KEYWORDS = [
    "chip", "module", "board", "is", "wifi", "bluetooth", "deep", "sleep", "jtag",
    "on", "off", "console", "usb", "nothing", "gpio", "input", "output",
    "open-drain", "adc", "dac", "interrupt", "pwm", "with", "no", "pull",
    "internal", "external", "off-board", "pull-up", "pull-down", "wakes", "from",
    "high", "low", "at", "reset",
]
_END = r"(?![A-Za-z0-9_-])"


def _kw_name(w: str) -> str:
    return w.upper().replace("-", "_")


_KW_BY_NAME = {_kw_name(w): w for w in _KEYWORDS}

_GRAMMAR = "\n".join(
    [
        ("start: chip_s | module_s | board_s | wifi_s | bt_s | sleep_s | jtag_s | console_s"
         " | reset_s | pin_s"),
        "chip_s: CHIP IS NAME",
        "module_s: MODULE IS NAME",
        "board_s: BOARD IS NAME",
        "wifi_s: WIFI IS onoff",
        "bt_s: BLUETOOTH IS onoff",
        "sleep_s: DEEP SLEEP IS onoff",
        "jtag_s: JTAG IS onoff",
        "onoff: ON | OFF",
        "console_s: CONSOLE IS ON (UARTN | USB | NOTHING)",
        "reset_s: GPIO INT IS level AT RESET",
        "level: HIGH | LOW",
        "pin_s: GPIO INT IS pin_mode role? pull? wake?",
        "pin_mode: plain_mode function? | PERIPH SIGNAL",
        "plain_mode: INPUT | OUTPUT | OPEN_DRAIN",
        "function: ADC | DAC | INTERRUPT | PWM",
        "role: STRING",
        "pull: WITH ((INTERNAL | EXTERNAL | OFF_BOARD) (PULL_UP | PULL_DOWN) | NO PULL)",
        "wake: WAKES FROM DEEP SLEEP ON level",
        *(f"{_kw_name(w)}: /{re.escape(w)}{_END}/" for w in _KEYWORDS),
        f"UARTN: /uart[0-2]{_END}/",
        f"PERIPH: /(i2c|spi|uart)[0-9]+{_END}/",
        f"SIGNAL: /(sda|scl|tx|rx|mosi|miso|sclk|cs){_END}/",
        f"NAME: /[a-z0-9][a-z0-9_-]*{_END}/",
        f"INT: /[0-9]+{_END}/",
        'STRING: /"[^"\\n]*"/',
        "%ignore /[ \\t\\r\\n]+/",
    ]
)

_PARSER = Lark(_GRAMMAR, parser="lalr", lexer="contextual", propagate_positions=True)

_DISPLAY = {
    "$END": "the end of the statement (a '.')",
    "NAME": "a name in lowercase letters, digits, '-' or '_'",
    "INT": "a number",
    "STRING": 'a quoted role such as "button"',
    "PERIPH": "a bus such as 'i2c0', 'spi2' or 'uart1'",
    "SIGNAL": "a signal ('sda', 'scl', 'tx', 'rx', 'mosi', 'miso', 'sclk' or 'cs')",
    "UARTN": "'uart0', 'uart1' or 'uart2'",
}

_PULLS = {
    ("internal", "up"): "internal_up",
    ("internal", "down"): "internal_down",
    ("external", "up"): "external_up",
    ("external", "down"): "external_down",
    ("off-board", "up"): "external_offboard",
    ("off-board", "down"): "external_offboard",
}
_PULL_TEXT = {
    "none": "with no pull",
    "internal_up": "with internal pull-up",
    "internal_down": "with internal pull-down",
    "external_up": "with external pull-up",
    "external_down": "with external pull-down",
    "external_offboard": "with off-board pull-up",
}
_SIGNALS = {
    "i2c": {"sda", "scl"},
    "uart": {"tx", "rx"},
    "spi": {"mosi", "miso", "sclk", "cs"},
}

# Features the language can state, with the question to ask when a spec doesn't.
_FEATURE_FIELDS = {
    "wifi": ("wifi", "Does the device use Wi-Fi?"),
    "bluetooth": ("bluetooth", "Does the device use Bluetooth?"),
    "deep sleep": ("deep_sleep", "Does the device use deep sleep?"),
    "jtag": ("jtag", "Is a JTAG debugger connected?"),
    "console": ("console", "Which port is the serial console on?"),
}


class CnlError(Exception):
    """One or more problems in a controlled-English spec."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("\n".join(e["message"] for e in errors))

    def to_list(self) -> list[dict]:
        return [dict(e) for e in self.errors]


def _err(line: int, column: int, message: str) -> dict:
    return {"line": line, "column": column, "message": message}


def _split(text: str) -> tuple[list[tuple[str, int, int]], list[dict]]:
    """Split into (statement text, line, column) at '.', skipping '#' comment lines."""
    stmts: list[tuple[str, int, int]] = []
    errors: list[dict] = []
    buf: list[str] = []
    start = (1, 1)
    line, col = 1, 1
    line_blank = True
    in_quote = False
    in_comment = False
    for ch in text:
        if ch == "\n":
            in_quote = in_comment = False
            line_blank = True
            if buf:
                buf.append(ch)
            line += 1
            col = 1
            continue
        if in_comment:
            col += 1
            continue
        if line_blank and ch == "#":
            in_comment = True
            col += 1
            continue
        if ch not in " \t\r":
            line_blank = False
        if ch == '"':
            in_quote = not in_quote
        if ch == "." and not in_quote:
            body = "".join(buf)
            if body.strip():
                stmts.append((body, *start))
            else:
                errors.append(_err(line, col, f"Line {line}: empty statement; remove the extra '.'."))
            buf = []
        else:
            if not buf:
                if ch in " \t\r":
                    col += 1
                    continue
                start = (line, col)
            buf.append(ch)
        col += 1
    if "".join(buf).strip():
        errors.append(
            _err(start[0], start[1], f"Line {start[0]}: this statement is missing its '.' at the end.")
        )
    return stmts, errors


def _display(name: str) -> str:
    if name in _DISPLAY:
        return _DISPLAY[name]
    return f"'{_KW_BY_NAME[name]}'" if name in _KW_BY_NAME else name.lower()


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " or " + items[-1]


def _syntax_error(e: UnexpectedInput, stmt: str, line: int, col: int) -> dict:
    if isinstance(e, UnexpectedToken):
        expected = set(e.accepts or e.expected)
        if e.token.type == "$END":
            found, eline, ecol = None, line + stmt.count("\n"), col
        else:
            found, eline, ecol = str(e.token), e.token.line, e.token.column
    else:
        assert isinstance(e, UnexpectedCharacters)
        expected = set(e.allowed or ())
        eline, ecol = e.line, e.column
        lines = ("\n" * (line - 1) + " " * (col - 1) + stmt).split("\n")
        m = re.match(r'"[^\s]*|[^\s]+', lines[eline - 1][ecol - 1 :])
        found = m.group(0) if m else "?"
    names = sorted(expected, key=lambda n: (n == "$END", n))
    if "CHIP" in expected:
        what = (
            "a statement starting with 'chip', 'module', 'board', 'gpio', 'wifi', "
            "'bluetooth', 'deep sleep', 'jtag' or 'console'"
        )
    else:
        what = _join([_display(n) for n in names])
        if "INPUT" in expected:
            what = "a mode: " + what
    msg = f"Line {eline}: expected {what}"
    if found is None:
        msg += " but the statement ended"
    elif found.startswith('"'):
        msg = f"Line {eline}: the quote opened at '{found}' is never closed"
        found = None
    else:
        msg += f" but found '{found}'"
    msg += "."
    if found is not None and found != found.lower():
        msg += " Keywords and names must be lowercase."
    return _err(eline, ecol, msg)


def _snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _toks(t: Tree) -> list[Token]:
    return [v for v in t.scan_values(lambda v: isinstance(v, Token))]


def parse(text: str) -> Manifest:
    stmts, errors = _split(text)
    hw: dict[str, str] = {}
    feat: dict[str, object] = {}
    pins: list[Pin] = []
    resets: list[tuple[int, str, int, int]] = []
    seen: set[str] = set()
    machines: list[dict] = []

    for stmt, line, col in stmts:
        if re.match(r"(state|machine)\b", stmt):
            _parse_fsm_stmt(stmt, line, col, machines, errors)
            continue
        padded = "\n" * (line - 1) + " " * (col - 1) + stmt
        try:
            tree = _PARSER.parse(padded).children[0]
        except UnexpectedInput as e:
            errors.append(_syntax_error(e, stmt, line, col))
            continue
        kind = tree.data
        toks = _toks(tree)

        def fail(msg: str, line: int = line, col: int = col) -> None:
            errors.append(_err(line, col, f"Line {line}: {msg}"))

        if kind in ("chip_s", "module_s", "board_s", "wifi_s", "bt_s", "sleep_s", "jtag_s",
                    "console_s"):
            if kind in seen:
                fail("this is stated twice; keep only one.")
                continue
            seen.add(kind)
        if kind in ("chip_s", "module_s", "board_s"):
            hw[kind[:-2]] = toks[-1].value
        elif kind in ("wifi_s", "bt_s", "sleep_s", "jtag_s"):
            key = {"wifi_s": "wifi", "bt_s": "bluetooth", "sleep_s": "deep_sleep", "jtag_s": "jtag"}
            feat[key[kind]] = toks[-1].value == "on"
        elif kind == "console_s":
            v = toks[-1].value
            feat["console"] = "none" if v == "nothing" else v
        elif kind == "reset_s":
            resets.append((int(toks[1].value), toks[3].value, line, col))
        else:
            p = _build_pin(tree, toks, fail)
            if p is not None:
                pins.append(p)

    for gpio, level, line, col in resets:
        hit = [i for i, p in enumerate(pins) if p.gpio == gpio]
        if not hit:
            errors.append(
                _err(line, col, f"Line {line}: GPIO {gpio} is said to be {level} at reset, "
                     f"but no 'gpio {gpio} is ...' statement declares it.")
            )
        for i in hit:
            pins[i] = _replace(pins[i], reset_level=level)

    for key, example in (("chip", "chip is esp32."), ("module", "module is esp32-wroom-32."),
                         ("board", "board is devkit-v1.")):
        if key not in hw:
            errors.append(_err(1, 1, f"Line 1: the spec never says its {key}; add a line such as '{example}'"))
    if errors:
        errors.sort(key=lambda e: (e["line"], e["column"]))
        raise CnlError(errors)
    return Manifest(
        chip=hw["chip"],
        module=hw["module"],
        board=hw["board"],
        idf_version=IDF_VERSION,
        features=Features(**feat),  # type: ignore[arg-type]
        pins=tuple(pins),
        state_machines=tuple(
            StateMachine(m["name"], m["start"], tuple(m["states"])) for m in machines
        ),
    )


_RESERVED = {"go", "to", "on", "every", "after", "is", "with", "do"}
_IDENT = re.compile(r"[a-z][a-z0-9_]*$")
_UNIT_MS = {"ms": 1, "s": 1000, "min": 60000}


def _fsm_name(word: str, what: str) -> str | None:
    """Return an error message if ``word`` can't be a machine or state name."""
    if word in _RESERVED:
        return f"'{word}' is a reserved word and can't be a {what} name."
    if not _IDENT.match(word):
        return (f"{what} name '{word}' must be lowercase letters, digits and '_', "
                "starting with a letter.")
    return None


def _fsm_phrase(words: list[str], what: str) -> tuple[str | None, str | None]:
    """Normalise an event or action phrase to snake_case; return (value, error)."""
    if not words:
        return None, f"the {what} is missing."
    for w in words:
        if w in _RESERVED:
            return None, f"'{w}' is a reserved word and can't be part of {what} '{' '.join(words)}'."
        if not re.match(r"[a-z0-9][a-z0-9_-]*$", w):
            return None, (f"{what} word '{w}' must be lowercase letters, digits, '-' or '_'.")
    v = _snake(" ".join(words))
    if not _IDENT.match(v):
        return None, f"{what} '{' '.join(words)}' must start with a letter."
    return v, None


def _parse_transition(words: list[str]) -> tuple[Transition | None, str | None]:
    def goto(rest: list[str], kind: str, count: int | None) -> tuple[Transition | None, str | None]:
        if len(rest) < 3 or rest[-3:-1] != ["go", "to"]:
            return None, f"'{kind}' transitions need 'go to STATE' at the end."
        event, err = _fsm_phrase(rest[:-3], "event")
        if err:
            return None, err
        target = rest[-1]
        if (err := _fsm_name(target, "state")) is not None:
            return None, err
        return Transition(kind=kind, event=event, target=target, count=count), None

    head = words[0]
    if head == "on":
        return goto(words[1:], "on", None)
    if head == "after" and len(words) >= 3 and words[2] in _UNIT_MS:
        # A time unit right after the number makes this a timer: 'after 20 s go to idle'.
        if len(words) != 6 or words[3:5] != ["go", "to"] or not re.match(r"[0-9]+$", words[1]):
            return None, "a timer must look like 'after 20 s go to idle' (units: ms, s or min)."
        ms = int(words[1]) * _UNIT_MS[words[2]]
        if ms < 1:
            return None, "the time after 'after' must be more than zero."
        if (err := _fsm_name(words[5], "state")) is not None:
            return None, err
        return Transition(kind="timeout", target=words[5], period_ms=ms), None
    if head == "after":
        if len(words) < 2 or not re.match(r"[0-9]+$", words[1]) or int(words[1]) < 1:
            return None, "'after' needs a whole number of at least 1, e.g. 'after 3 read failed go to error'."
        return goto(words[2:], "after", int(words[1]))
    if head == "every":
        if (len(words) < 5 or not re.match(r"[0-9]+$", words[1]) or words[2] not in _UNIT_MS
                or words[3] != "do"):
            return None, ("'every' must look like 'every 10 s do read sensor' "
                          "(units: ms, s or min).")
        ms = int(words[1]) * _UNIT_MS[words[2]]
        if ms < 1:
            return None, "the period after 'every' must be more than zero."
        action, err = _fsm_phrase(words[4:], "action")
        if err:
            return None, err
        return Transition(kind="every", period_ms=ms, action=action), None
    return None, f"a transition starts with 'on', 'every' or 'after', not '{head}'."


def _parse_fsm_stmt(stmt: str, line: int, col: int, machines: list[dict], errors: list[dict]) -> None:
    def fail(msg: str) -> None:
        errors.append(_err(line, col, f"Line {line}: {msg}"))

    text = " ".join(stmt.split())
    if text.startswith("machine"):
        m = re.match(r"machine (\S+) starts in (\S+)$", text)
        if not m:
            fail("expected 'machine NAME starts in STATE', e.g. 'machine sensor starts in idle'.")
            return
        for w, what in ((m.group(1), "machine"), (m.group(2), "state")):
            if (err := _fsm_name(w, what)) is not None:
                fail(err)
                return
        machines.append({"name": m.group(1), "start": m.group(2), "states": []})
        return
    head, colon, body = text.partition(":")
    hw = head.split()
    if len(hw) != 2 or hw[0] != "state":
        fail("expected 'state NAME: TRANSITION; ...', e.g. 'state idle: on button press go to measuring'.")
        return
    if (err := _fsm_name(hw[1], "state")) is not None:
        fail(err)
        return
    if not machines:
        fail("a state needs a machine first; add 'machine NAME starts in STATE.' above it.")
        return
    transitions: list[Transition] = []
    if colon:
        for part in body.split(";"):
            words = part.split()
            if not words:
                fail(f"state '{hw[1]}' has an empty transition (check for a stray ';' or ':').")
                return
            t, err = _parse_transition(words)
            if err:
                fail(f"state '{hw[1]}': {err}")
                return
            assert t is not None
            transitions.append(t)
    machines[-1]["states"].append(State(hw[1], tuple(transitions)))


def _duration_text(ms: int) -> str:
    for unit in ("min", "s"):
        if ms % _UNIT_MS[unit] == 0:
            return f"{ms // _UNIT_MS[unit]} {unit}"
    return f"{ms} ms"


def _transition_text(t: Transition) -> str:
    if t.kind == "every":
        return f"every {_duration_text(t.period_ms or 0)} do {(t.action or '').replace('_', ' ')}"
    if t.kind == "timeout":
        return f"after {_duration_text(t.period_ms or 0)} go to {t.target}"
    ev = (t.event or "").replace("_", " ")
    if t.kind == "after":
        return f"after {t.count} {ev} go to {t.target}"
    return f"on {ev} go to {t.target}"


def _replace(p: Pin, **kw: object) -> Pin:
    d = {**p.__dict__, **kw}
    return Pin(**d)


def _build_pin(tree: Tree, toks: list[Token], fail) -> Pin | None:
    gpio = int(toks[1].value)
    label = f"GPIO {gpio}"
    role = bus = wake = None
    pull = None
    function = "gpio"
    sub: dict[str, Tree] = {}
    for c in tree.children:
        if isinstance(c, Tree):
            sub[c.data] = c
    mode_tree = sub["pin_mode"]
    mt = _toks(mode_tree)
    if mt[0].type == "PERIPH":
        periph, sig = mt[0].value, mt[1].value
        kind = re.match(r"[a-z0-9]+?(?=\d+$)", periph).group(0)  # type: ignore[union-attr]
        if sig not in _SIGNALS[kind]:
            fail(f"{label}: '{periph} {sig}' is not a signal of {kind}; use {_join(sorted(_SIGNALS[kind]))}.")
            return None
        function, bus = kind, f"{periph}_{sig}"
        mode = "open_drain" if kind == "i2c" else "input" if sig in ("rx", "miso") else "output"
    else:
        mode = mt[0].value.replace("-", "_")
        if len(mt) > 1:
            function = mt[1].value
    if "role" in sub:
        role = _snake(_toks(sub["role"])[0].value.strip('"'))
        if not role:
            fail(f"{label}: the role in quotes needs at least one letter or digit.")
            return None
    elif bus is None:
        fail(f"{label}: say what it is for, in quotes, e.g. gpio {gpio} is {mode} \"button\" ...")
        return None
    if role is None:
        role = bus
    if "pull" in sub:
        pt = [t.value for t in _toks(sub["pull"])]
        pull = "none" if pt[1] == "no" else _PULLS[(pt[1], pt[2].split("-")[1])]
    else:
        fail(f"{label}: say which pull it has, e.g. 'with internal pull-up', "
             "'with external pull-up' or 'with no pull'.")
        return None
    if "wake" in sub:
        wake = _toks(sub["wake"])[-1].value
    assert role is not None
    return Pin(gpio=gpio, role=role, mode=mode, pull=pull, function=function, bus=bus,
               wake=wake, source=None)


def render(m: Manifest) -> str:
    f = m.features

    def onoff(b: bool) -> str:
        return "on" if b else "off"

    lines = [
        f"chip is {m.chip}.",
        f"module is {m.module}.",
        f"board is {m.board}.",
        f"wifi is {onoff(f.wifi)}.",
        f"bluetooth is {onoff(f.bluetooth)}.",
        f"deep sleep is {onoff(f.deep_sleep)}.",
        f"jtag is {onoff(f.jtag)}.",
        f"console is on {'nothing' if f.console == 'none' else f.console}.",
    ]
    for p in m.pins:
        if p.bus:
            periph, sig = p.bus.split("_", 1)
            parts = [f"gpio {p.gpio} is {periph} {sig}"]
        else:
            parts = [f"gpio {p.gpio} is {p.mode.replace('_', '-')}"]
            if p.function != "gpio":
                parts.append(p.function)
        if p.role != p.bus:
            parts.append(f'"{p.role}"')
        parts.append(_PULL_TEXT[p.pull])
        if p.wake:
            parts.append(f"wakes from deep sleep on {p.wake}")
        lines.append(" ".join(parts) + ".")
    done: set[int] = set()
    for p in m.pins:
        if p.reset_level and p.gpio not in done:
            done.add(p.gpio)
            lines.append(f"gpio {p.gpio} is {p.reset_level} at reset.")
    for sm in m.state_machines:
        lines.append(f"machine {sm.name} starts in {sm.start}.")
        for st in sm.states:
            if st.transitions:
                lines.append(f"state {st.name}: "
                             + "; ".join(_transition_text(t) for t in st.transitions) + ".")
            else:
                lines.append(f"state {st.name}.")
    return "\n".join(lines) + "\n"


def assumptions(m: Manifest, text: str | None = None) -> list[dict]:
    """Facts the manifest holds that the spec never stated.

    Needs the spec text to know what was stated; with ``text=None`` returns [].
    """
    if text is None:
        return []
    stmts, _ = _split(text)
    stated = {s.strip() for s, _, _ in stmts}
    out = []
    for lead, (field_name, question) in _FEATURE_FIELDS.items():
        if not any(re.match(re.escape(lead) + r"\s+is\b", s) for s in stated):
            out.append({"field": f"features.{field_name}",
                        "value": getattr(m.features, field_name), "question": question})
    return out
