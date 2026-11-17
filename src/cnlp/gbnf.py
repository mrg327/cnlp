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
reset ::= "gpio " num " is " level " at reset.\n"
pin ::= "gpio " num " is " pmode role? pull? wake? ".\n"
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


def grammar() -> str:
    """The GBNF text to send as the model's ``grammar`` field."""
    lines = _nest("line", MAX_LINES)
    return (_RULES.replace("LINES", lines).replace("COMMENT", str(MAX_COMMENT))).strip() + "\n"


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
