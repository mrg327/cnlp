"""GBNF grammar for the controlled-English translation output.

The language itself is defined by the parser in cnl.py; this grammar is a
stricter, line-oriented subset that is easy for a model to emit. Keyword lists
are defined here and tests/test_gbnf.py guards against drift by checking that
every canonical render parses under this grammar. The grammar needs the END
terminator and bounded repetition or the model loops until max_tokens.
"""

from __future__ import annotations

import re

MAX_LINES = 60
MAX_COMMENT = 200

_RULES = r'''
root ::= LINES "END"
line ::= comment | stmt
comment ::= "#" [^\n]{0,COMMENT} "\n"
stmt ::= hw | feature | pin | reset | machine | state
name ::= [a-z0-9] [a-z0-9_-]{0,30}
ident ::= [a-z] [a-z0-9_]{0,30}
num ::= [0-9] [0-9]?
hw ::= ("chip" | "module" | "board") " is " name ".\n"
feature ::= ("wifi" | "bluetooth" | "jtag" | "deep sleep") " is " onoff ".\n" | "console is on " port ".\n"
onoff ::= "on" | "off"
port ::= "uart0" | "uart1" | "uart2" | "usb" | "nothing"
level ::= "high" | "low"
reset ::= "gpio " gnum " is " level " at reset.\n"
pin ::= "gpio " gnum " is " pmode role? pull? wake? ".\n"
pmode ::= plain (" " func)? | bus " " signal
plain ::= "input" | "output" | "open-drain"
func ::= "adc" | "dac" | "interrupt" | "pwm"
bus ::= ("i2c" | "spi" | "uart") [0-9]
signal ::= "sda" | "scl" | "tx" | "rx" | "mosi" | "miso" | "sclk" | "cs"
role ::= " \"" [a-zA-Z0-9 _-]{1,30} "\""
pull ::= " with " (("internal" | "external" | "off-board") " " ("pull-up" | "pull-down") | "no pull")
wake ::= " wakes from deep sleep on " level
machine ::= "machine " ident " starts in " ident ".\n"
state ::= "state " ident (": " trans (("; ") trans){0,7})? ".\n"
trans ::= "on " words " go to " ident | "after " num " " words " go to " ident | "every " num " " unit " do " words
unit ::= "ms" | "s" | "min"
word ::= [a-z0-9] [a-z0-9_-]{0,20}
words ::= word (" " word){0,5}
'''


def grammar(allowed_gpios: set[int] | None = None, *, fsm: bool = True) -> str:
    """The GBNF text to send as the model's ``grammar`` field.

    ``allowed_gpios``: when given (non-empty), a pin statement can only use those
    GPIO numbers, so the model cannot invent a pin the request never names.
    ``fsm=False`` drops machine/state lines (a separate staged call writes them).
    """
    lines = _nest("line", MAX_LINES)
    g = (_RULES.replace("LINES", lines).replace("COMMENT", str(MAX_COMMENT))).strip() + "\n"
    if allowed_gpios:
        alts = " | ".join(f'"{n}"' for n in sorted(allowed_gpios))
        g += f"gnum ::= {alts}\n"
    else:
        g += "gnum ::= num\n"
    if not fsm:
        g = g.replace("stmt ::= hw | feature | pin | reset | machine | state",
                      "stmt ::= hw | feature | pin | reset")
        g = "\n".join(ln for ln in g.splitlines() if not ln.startswith(("machine ::=", "state ::=",
                                                                          "trans ::=", "unit ::=")))
        g += "\n"
    return g


def fsm_grammar(machines: list[dict], numbers: set[int] | None = None,
                 units: set[str] | None = None) -> str:
    """Grammar for the machine/state lines of already-planned machines.

    Each machine is ``{name, states, start, events, actions}`` with snake_case
    names. The output is forced to declare each machine, then every one of its
    states once in the given order; transition targets, events and actions can
    only be the planned ones. ``numbers`` limits counts and periods to numbers the
    request states (empty: no ``after``/``every`` transitions at all); ``units`` limits
    ``every`` to the time units the request uses (ms, s, min).
    """

    def lit(s: str) -> str:
        return '"' + s.replace("_", " ") + '"'

    nl_ = '"\\n"'
    rules = [f'comment ::= "#" [^\\n]{{0,{MAX_COMMENT}}} {nl_}',
             "unit ::= " + " | ".join(f'"{u}"' for u in ("ms", "s", "min") if not units or u in units)]
    if numbers is None:
        rules.append("num ::= [0-9] [0-9]?")
    elif numbers:
        rules.append("num ::= " + " | ".join(f'"{n}"' for n in sorted(numbers)))
    body: list[str] = []
    for i, m in enumerate(machines):
        rules.append(f"st{i} ::= " + " | ".join(f'"{s}"' for s in m["states"]))
        trans = []
        if m["events"]:
            rules.append(f"ev{i} ::= " + " | ".join(lit(e) for e in m["events"]))
            trans.append(f'"on " ev{i} " go to " st{i}')
            if numbers is None or numbers:
                trans.append(f'"after " num " " ev{i} " go to " st{i}')
        if m["actions"] and (numbers is None or numbers):
            rules.append(f"ac{i} ::= " + " | ".join(lit(a) for a in m["actions"]))
            trans.append(f'"every " num " " unit " do " ac{i}')
        if trans:
            rules.append(f"tr{i} ::= " + " | ".join(trans))
            tail = f'(": " tr{i} ("; " tr{i}){{0,7}})?'
        else:
            tail = ""
        body.append(f'"machine {m["name"]} starts in {m["start"]}." {nl_}')
        for s in m["states"]:
            body.append(f'comment? "state {s}" {tail} "." {nl_}')
    return "\n".join([f'root ::= {" ".join(body)} "END"', *rules]) + "\n"


def _nest(rule: str, n: int) -> str:
    """``(r (r (r)?)?)?`` -- bounded repetition without relying on {m,n}."""
    out = ""
    for _ in range(n):
        out = f"({rule}{' ' + out if out else ''})?"
    return out


# ---- GBNF -> Lark converter, used by tests to check the grammar ----------------

_TOK = re.compile(
    r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<cls>\[(?:[^\]\\]|\\.)*\])|(?P<rep>\{\d+(?:,\d+)?\})'
    r"|(?P<name>[A-Za-z][A-Za-z0-9_-]*)|(?P<op>[()|*+?]))"
)


def _to_lark(g: str | None = None) -> str:
    """Convert the grammar (or any GBNF using this subset) to Lark EBNF text."""
    g = g or grammar()
    out = []
    for raw in g.splitlines():
        if not raw.strip():
            continue
        name, _, body = raw.partition("::=")
        name = name.strip().replace("-", "_")
        name = "start" if name == "root" else name
        pos, parts = 0, []
        body = body.strip()
        while pos < len(body):
            m = _TOK.match(body, pos)
            if not m or m.end() == pos:
                raise ValueError(f"cannot convert: {raw!r} at {pos}")
            pos = m.end()
            if m["str"]:
                parts.append(m["str"])
            elif m["cls"]:
                parts.append("/" + m["cls"].replace("/", "\\/") + "/")
            elif m["rep"]:
                lo, _, hi = m["rep"][1:-1].partition(",")
                parts.append(f"~{lo}..{hi}" if hi else f"~{lo}")
            elif m["name"]:
                parts.append(m["name"].replace("-", "_"))
            else:
                parts.append(m["op"])
        out.append(f"{name}: {' '.join(parts)}")
    return "\n".join(out)
