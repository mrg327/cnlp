"""Static pin extraction from existing ESP-IDF C code (read-only, tree-sitter).

`extract(target, sources)` parses every `.c`/`.h` under `target/<source>` and
recovers which pins the code configures: `gpio_config_t` initializers plus the
`gpio_config(&x)` call, `gpio_set_direction` / pull / reset calls, and driver
config structs (I2S std/tdm/pdm, I2C master and legacy, UART `uart_set_pin`,
LEDC, PCNT, RMT, SPI bus and device).

It evaluates a small, safe subset of C: `#define` constants, enums, `const`
scalars, `const` arrays with `arr[i]`, `1ULL << X`, `BIT64(X)`, `|` and the
loop idiom `for (i = 0; i < N; i++) mask |= 1ULL << arr[i];` (unrolled up to
64 iterations). `#if NAME` is honored when NAME resolves. Anything it cannot
resolve becomes an `Unresolved` entry with file:line and the source text; it
never guesses.

Limits: no preprocessor beyond object-like `#define` and simple `#if`; no
pointers, function parameters, struct passing between functions, or runtime
values; `gpio_set_level` and other non-configuring calls are ignored; macro
bodies are not scanned for calls; a name defined in two files resolves to the
file's own definition first, then any other file's.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tree_sitter_c
from tree_sitter import Language, Node, Parser

_PARSER = Parser(Language(tree_sitter_c.language()))
SKIP_DIRS = frozenset({"build", "managed_components"})
MAX_UNROLL = 64

MODES = {
    "GPIO_MODE_DISABLE": "disable",
    "GPIO_MODE_INPUT": "input",
    "GPIO_MODE_OUTPUT": "output",
    "GPIO_MODE_OUTPUT_OD": "output_od",
    "GPIO_MODE_INPUT_OUTPUT_OD": "input_output_od",
    "GPIO_MODE_INPUT_OUTPUT": "input_output",
}
PULL_MODES = {  # gpio_set_pull_mode argument -> (up, down)
    "GPIO_PULLUP_ONLY": (True, False),
    "GPIO_PULLDOWN_ONLY": (False, True),
    "GPIO_PULLUP_PULLDOWN": (True, True),
    "GPIO_FLOATING": (False, False),
}
KNOWN_INTS = {
    "GPIO_PULLUP_ENABLE": 1,
    "GPIO_PULLUP_DISABLE": 0,
    "GPIO_PULLDOWN_ENABLE": 1,
    "GPIO_PULLDOWN_DISABLE": 0,
    "GPIO_NUM_NC": -1,
    "I2S_GPIO_UNUSED": -1,
    "UART_PIN_NO_CHANGE": -1,
    "true": 1,
    "false": 0,
    "NULL": 0,
}
_I2S_GPIO = {
    f"gpio_cfg.{s}": ("i2s", s) for s in ("mclk", "bclk", "ws", "dout", "din")
}
# struct type -> {field path: (driver, signal)}
DRIVER_STRUCTS: dict[str, dict[str, tuple[str, str]]] = {
    "i2s_std_config_t": _I2S_GPIO,
    "i2s_tdm_config_t": _I2S_GPIO,
    "i2s_pdm_tx_config_t": {
        "gpio_cfg.clk": ("i2s", "clk"),
        "gpio_cfg.dout": ("i2s", "dout"),
    },
    "i2s_pdm_rx_config_t": {"gpio_cfg.clk": ("i2s", "clk"), "gpio_cfg.din": ("i2s", "din")},
    "i2c_master_bus_config_t": {"sda_io_num": ("i2c", "sda"), "scl_io_num": ("i2c", "scl")},
    "i2c_config_t": {"sda_io_num": ("i2c", "sda"), "scl_io_num": ("i2c", "scl")},
    "ledc_channel_config_t": {"gpio_num": ("ledc", "out")},
    "rmt_tx_channel_config_t": {"gpio_num": ("rmt", "tx")},
    "rmt_rx_channel_config_t": {"gpio_num": ("rmt", "rx")},
    "pcnt_chan_config_t": {
        "edge_gpio_num": ("pcnt", "edge"),
        "level_gpio_num": ("pcnt", "level"),
    },
    "spi_bus_config_t": {
        "mosi_io_num": ("spi", "mosi"),
        "data0_io_num": ("spi", "mosi"),
        "miso_io_num": ("spi", "miso"),
        "data1_io_num": ("spi", "miso"),
        "sclk_io_num": ("spi", "sclk"),
        "quadwp_io_num": ("spi", "wp"),
        "data2_io_num": ("spi", "wp"),
        "quadhd_io_num": ("spi", "hd"),
        "data3_io_num": ("spi", "hd"),
    },
    "spi_device_interface_config_t": {"spics_io_num": ("spi", "cs")},
}
# (type, path) pairs whose truthy value turns on an internal pull-up on that bus's pins
PULLUP_FLAGS = {
    "i2c_master_bus_config_t": {"flags.enable_internal_pullup": ("sda_io_num", "scl_io_num")},
    "i2c_config_t": {"sda_pullup_en": ("sda_io_num",), "scl_pullup_en": ("scl_io_num",)},
}


@dataclass
class Use:
    kind: str  # gpio_config | set_direction | set_pull | pullup | pulldown | reset | driver
    file: str
    line: int
    function: str = ""
    mode: str | None = None  # input | output | input_output | output_od | ... | disable
    pull_up: bool | None = None
    pull_down: bool | None = None
    intr: str | None = None
    driver: str | None = None
    signal: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


@dataclass
class Unresolved:
    file: str
    line: int
    text: str
    what: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Extracted:
    pins: dict[int, list[Use]] = field(default_factory=dict)
    unresolved: list[Unresolved] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "pins": {str(p): [u.to_dict() for u in us] for p, us in sorted(self.pins.items())},
            "unresolved": [u.to_dict() for u in self.unresolved],
        }


class _Unres(Exception):
    """A value could not be determined statically."""


class _Poison:
    """Marks a local whose value is unknown (e.g. a loop variable we cannot unroll)."""


@dataclass
class _Field:
    value: int | None
    sym: str | None
    line: int
    text: str


@dataclass
class _Struct:
    type: str
    fields: dict[str, _Field] = field(default_factory=dict)
    line: int = 0


def _txt(n: Node | None) -> str:
    return (n.text or b"").decode("utf-8", "replace") if n is not None else ""


def _strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"//.*", "", s)
    return s.replace("\\\n", " ").strip()


class _Defs:
    """Global-ish definitions: per file, then merged."""

    def __init__(self) -> None:
        self.macros: dict[str | None, dict[str, str]] = {None: {}}
        self.consts: dict[str | None, dict[str, Node]] = {None: {}}
        self.arrays: dict[str | None, dict[str, list[Node | None]]] = {None: {}}
        self.enums: dict[str | None, dict[str, int]] = {None: {}}
        self.func_macros: set[str] = set()

    def put(self, table: str, file: str, name: str, value: Any) -> None:
        t = getattr(self, table)
        t.setdefault(file, {})[name] = value
        t[None].setdefault(name, value)

    def get(self, table: str, file: str, name: str) -> Any:
        t = getattr(self, table)
        if name in t.get(file, {}):
            return t[file][name]
        return t[None].get(name)


class _Extractor:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.defs = _Defs()
        self.out = Extracted()
        self.keep: list[Any] = []  # keep trees alive
        self._macro_cache: dict[str, Node] = {}
        self._depth = 0

    # ---- evaluation -------------------------------------------------

    def _macro_node(self, text: str) -> Node:
        n = self._macro_cache.get(text)
        if n is None:
            src = f"int __m = {text};".encode()
            tree = _PARSER.parse(src)
            self.keep.append(tree)
            decl = tree.root_node.children[0]
            init = decl.child_by_field_name("declarator")
            n = init.child_by_field_name("value") if init is not None else None
            if n is None:
                raise _Unres(text)
            self._macro_cache[text] = n
        return n

    def ev(self, n: Node | None, file: str, env: dict[str, Any]) -> int:
        if n is None:
            raise _Unres("")
        self._depth += 1
        try:
            if self._depth > 40:
                raise _Unres("recursion")
            return self._ev(n, file, env)
        finally:
            self._depth -= 1

    def _ev(self, n: Node, file: str, env: dict[str, Any]) -> int:
        t = n.type
        if t == "number_literal":
            s = re.sub(r"[uUlL]+$", "", _txt(n))
            try:
                return int(s, 0)
            except ValueError:
                if re.fullmatch(r"0[0-7]+", s):
                    return int(s, 8)
                raise _Unres(_txt(n)) from None
        if t in ("true", "false"):
            return int(t == "true")
        if t == "char_literal":
            raise _Unres(_txt(n))
        if t == "parenthesized_expression":
            return self.ev(n.named_children[0], file, env)
        if t == "cast_expression":
            return self.ev(n.child_by_field_name("value"), file, env)
        if t == "identifier":
            return self._ident(_txt(n), file, env)
        if t == "unary_expression":
            op = _txt(n.child_by_field_name("operator"))
            v = self.ev(n.child_by_field_name("argument"), file, env)
            if op == "-":
                return -v
            if op == "+":
                return v
            if op == "~":
                return ~v
            if op == "!":
                return int(not v)
            raise _Unres(_txt(n))
        if t == "binary_expression":
            op = _txt(n.child_by_field_name("operator"))
            a = self.ev(n.child_by_field_name("left"), file, env)
            b = self.ev(n.child_by_field_name("right"), file, env)
            return _binop(op, a, b, _txt(n))
        if t == "call_expression":
            fn = _txt(n.child_by_field_name("function"))
            args = [c for c in n.child_by_field_name("arguments").named_children]
            if fn in ("BIT64", "BIT", "_BV") and len(args) == 1:
                return 1 << self.ev(args[0], file, env)
            raise _Unres(_txt(n))
        if t == "subscript_expression":
            arr = n.child_by_field_name("argument")
            idx = self.ev(n.child_by_field_name("indices") or n.named_children[1], file, env)
            if arr is None or arr.type != "identifier":
                raise _Unres(_txt(n))
            name = _txt(arr)
            items = env.get(name)
            if not isinstance(items, list):
                items = self.defs.get("arrays", file, name)
            if not isinstance(items, list):
                raise _Unres(_txt(n))
            if not 0 <= idx < len(items) or items[idx] is None:
                raise _Unres(_txt(n))
            el = items[idx]
            return el if isinstance(el, int) else self.ev(el, file, {})
        raise _Unres(_txt(n))

    def _ident(self, name: str, file: str, env: dict[str, Any]) -> int:
        if name in env:
            v = env[name]
            if isinstance(v, int):
                return v
            raise _Unres(name)
        c = self.defs.get("consts", file, name)
        if c is not None:
            return self.ev(c, file, {})
        e = self.defs.get("enums", file, name)
        if e is not None:
            return e
        m = self.defs.get("macros", file, name)
        if m is not None:
            if not m:
                raise _Unres(name)
            return self.ev(self._macro_node(m), file, {})
        mm = re.fullmatch(r"GPIO_NUM_(\d+)", name)
        if mm:
            return int(mm.group(1))
        mm = re.fullmatch(r"GPIO_SEL_(\d+)", name)
        if mm:
            return 1 << int(mm.group(1))
        if name in KNOWN_INTS:
            return KNOWN_INTS[name]
        raise _Unres(name)

    def sym(self, n: Node | None, file: str) -> str | None:
        """Identifier text after following `#define A B` aliases, else None."""
        if n is None:
            return None
        if n.type == "parenthesized_expression" and n.named_children:
            return self.sym(n.named_children[0], file)
        if n.type != "identifier":
            return None
        name = _txt(n)
        for _ in range(6):
            m = self.defs.get("macros", file, name)
            if m and re.fullmatch(r"[A-Za-z_]\w*", m):
                name = m
            else:
                break
        return name

    # ---- collection -------------------------------------------------

    def collect(self, rel: str, root: Node, src: bytes) -> list[Node]:
        """Record defines, enums, consts, arrays. Returns top-level declarations."""
        decls: list[Node] = []

        def visit(n: Node) -> None:
            t = n.type
            if t == "preproc_def":
                name = _txt(n.child_by_field_name("name"))
                val = _strip_comments(_txt(n.child_by_field_name("value")))
                self.defs.put("macros", rel, name, val)
                return
            if t == "preproc_function_def":
                self.defs.func_macros.add(_txt(n.child_by_field_name("name")))
                return
            if t == "function_definition":
                return
            if t == "enum_specifier":
                nxt = 0
                for e in _find(n, "enumerator"):
                    name = _txt(e.child_by_field_name("name"))
                    v = e.child_by_field_name("value")
                    try:
                        nxt = self.ev(v, rel, {}) if v is not None else nxt
                    except _Unres:
                        break
                    self.defs.put("enums", rel, name, nxt)
                    nxt += 1
            if t == "declaration":
                self._collect_decl(rel, n)
                decls.append(n)
                for c in n.children:
                    if c.type in ("enum_specifier",):
                        visit(c)
                return
            for c in n.children:
                visit(c)

        visit(root)
        return decls

    def _collect_decl(self, rel: str, n: Node) -> None:
        is_const = any(c.type == "type_qualifier" and _txt(c) == "const" for c in n.children)
        for d in n.children_by_field_name("declarator"):
            if d.type != "init_declarator":
                continue
            decl = d.child_by_field_name("declarator")
            val = d.child_by_field_name("value")
            if decl is None or val is None:
                continue
            if decl.type == "array_declarator" and val.type == "initializer_list":
                name = _txt(_inner_ident(decl))
                self.defs.put("arrays", rel, name, self._array_items(val, rel))
            elif decl.type == "identifier" and is_const and val.type != "initializer_list":
                self.defs.put("consts", rel, _txt(decl), val)

    def _array_items(self, init: Node, file: str) -> list[Node | int | None]:
        items: list[Node | int | None] = []
        for c in init.named_children:
            if c.type in ("comment",):
                continue
            if c.type == "initializer_pair":
                items.append(None)
                continue
            items.append(c)
        return items

    # ---- driver ----------------------------------------------------

    def run(self, files: list[Path]) -> Extracted:
        parsed: list[tuple[str, Node, bytes]] = []
        decls: dict[str, list[Node]] = {}
        for p in files:
            rel = p.relative_to(self.target).as_posix()
            src = p.read_bytes()
            tree = _PARSER.parse(src)
            self.keep.append(tree)
            parsed.append((rel, tree.root_node, src))
            self.out.files.append(rel)
        for rel, root, src in parsed:
            decls[rel] = self.collect(rel, root, src)
        for rel, root, _ in parsed:
            for fn in _find(root, "function_definition"):
                name = _txt(_inner_ident(fn.child_by_field_name("declarator")))
                body = fn.child_by_field_name("body")
                scope = _Scope(self, rel, name)
                scope.exec(body)
                scope.finish()
            top = _Scope(self, rel, "")
            for d in decls[rel]:
                top.exec(d)
            top.finish()
        for us in self.out.pins.values():
            us.sort(key=lambda u: (u.file, u.line))
        return self.out

    def add(self, pin: int, use: Use) -> None:
        self.out.pins.setdefault(pin, []).append(use)

    def unres(self, file: str, node: Node | int, text: str, what: str) -> None:
        line = node if isinstance(node, int) else node.start_point[0] + 1
        self.out.unresolved.append(Unresolved(file, line, " ".join(text.split())[:160], what))


def _find(n: Node, type_: str) -> list[Node]:
    out, stack = [], [n]
    while stack:
        x = stack.pop()
        if x.type == type_:
            out.append(x)
        stack.extend(reversed(x.children))
    return out


def _inner_ident(n: Node | None) -> Node | None:
    while n is not None and n.type != "identifier":
        nxt = n.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in n.named_children if c.type in (
                "identifier", "function_declarator", "pointer_declarator", "array_declarator",
                "parenthesized_declarator")), None)
        n = nxt
    return n


def _binop(op: str, a: int, b: int, text: str) -> int:
    try:
        return {
            "<<": lambda: a << b, ">>": lambda: a >> b, "|": lambda: a | b, "&": lambda: a & b,
            "^": lambda: a ^ b, "+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b,
            "/": lambda: a // b, "%": lambda: a % b,
            "<": lambda: int(a < b), "<=": lambda: int(a <= b), ">": lambda: int(a > b),
            ">=": lambda: int(a >= b), "==": lambda: int(a == b), "!=": lambda: int(a != b),
            "&&": lambda: int(bool(a and b)), "||": lambda: int(bool(a or b)),
        }[op]()
    except (KeyError, ZeroDivisionError, ValueError):
        raise _Unres(text) from None


class _Scope:
    """Interprets one function body (or the top level) in source order."""

    def __init__(self, ex: _Extractor, file: str, func: str) -> None:
        self.ex, self.file, self.func = ex, file, func
        self.env: dict[str, Any] = {}

    # --- helpers
    def _field(self, n: Node | None) -> _Field:
        ex = self.ex
        line = (n.start_point[0] + 1) if n is not None else 0
        try:
            v: int | None = ex.ev(n, self.file, self.env)
        except _Unres:
            v = None
        return _Field(v, ex.sym(n, self.file), line, _txt(n))

    def _flatten(self, st: _Struct, init: Node, prefix: str = "") -> None:
        for c in init.named_children:
            if c.type != "initializer_pair":
                continue
            des = c.child_by_field_name("designator")
            if des is None:
                continue
            ds = [_txt(x) for x in _find(c, "field_identifier") if x.parent is not None and x.parent.type == "field_designator" and x.parent.parent == c]
            if not ds:
                continue
            path = prefix + ".".join(ds)
            val = c.child_by_field_name("value")
            if val is not None and val.type == "initializer_list":
                self._flatten(st, val, path + ".")
            else:
                st.fields[path] = self._field(val)

    def _struct_from_decl(self, type_name: str, init: Node | None, line: int) -> _Struct:
        st = _Struct(type_name, line=line)
        if init is not None and init.type == "initializer_list":
            self._flatten(st, init)
        return st

    # --- statements
    def exec(self, n: Node | None) -> None:
        if n is None:
            return
        t = n.type
        if t == "declaration":
            self._decl(n)
        elif t == "for_statement":
            self._for(n)
        elif t == "preproc_if":
            cond = n.child_by_field_name("condition")
            try:
                taken = bool(self.ex.ev(cond, self.file, self.env))
            except _Unres:
                taken = None
            alt = n.child_by_field_name("alternative")
            body = [c for c in n.named_children if c != cond and c != alt and c.type != "comment"]
            if taken is None:
                for c in body + ([alt] if alt else []):
                    self.exec(c)
            elif taken:
                for c in body:
                    self.exec(c)
            elif alt is not None:
                self.exec(alt)
        elif t == "preproc_else":
            for c in n.named_children:
                self.exec(c)
        elif t == "assignment_expression":
            self._assign(n)
        elif t == "call_expression":
            self._call(n)
            args = n.child_by_field_name("arguments")
            if args is not None:
                for c in args.named_children:
                    if c.type == "call_expression":
                        self.exec(c)
        elif t in ("function_definition",):
            return
        else:
            for c in n.children:
                self.exec(c)

    def _decl(self, n: Node) -> None:
        ex = self.ex
        type_name = _txt(n.child_by_field_name("type"))
        for d in n.children_by_field_name("declarator"):
            decl = d.child_by_field_name("declarator") if d.type == "init_declarator" else d
            val = d.child_by_field_name("value") if d.type == "init_declarator" else None
            if decl is None:
                continue
            if decl.type == "array_declarator":
                name = _txt(_inner_ident(decl))
                if val is not None and val.type == "initializer_list":
                    items: list[Any] = []
                    for it in ex._array_items(val, self.file):
                        if it is None:
                            items.append(None)
                            continue
                        try:
                            items.append(ex.ev(it, self.file, self.env))
                        except _Unres:
                            items.append(None)
                    self.env[name] = items
                continue
            if decl.type != "identifier":
                continue
            name = _txt(decl)
            if type_name in DRIVER_STRUCTS or type_name == "gpio_config_t":
                self.env[name] = self._struct_from_decl(type_name, val, n.start_point[0] + 1)
            elif val is not None and val.type != "initializer_list":
                try:
                    self.env[name] = ex.ev(val, self.file, self.env)
                except _Unres:
                    self.env[name] = _Poison()
            else:
                self.env[name] = _Poison()

    def _lhs_path(self, left: Node) -> tuple[str, str] | None:
        parts: list[str] = []
        cur = left
        while cur.type == "field_expression":
            if _txt(cur.child_by_field_name("operator")) != ".":
                return None
            parts.append(_txt(cur.child_by_field_name("field")))
            cur = cur.child_by_field_name("argument")
        if cur.type != "identifier":
            return None
        return _txt(cur), ".".join(reversed(parts))

    def _assign(self, n: Node) -> None:
        ex = self.ex
        left = n.child_by_field_name("left")
        right = n.child_by_field_name("right")
        op = _txt(n.child_by_field_name("operator"))
        if left is None or right is None:
            return
        if left.type == "identifier":
            name = _txt(left)
            try:
                rv = ex.ev(right, self.file, self.env)
                if op == "=":
                    self.env[name] = rv
                else:
                    cur = ex.ev(left, self.file, self.env)
                    self.env[name] = _binop(op[:-1], cur, rv, _txt(n))
            except _Unres:
                self.env[name] = _Poison()
        elif left.type == "field_expression":
            lp = self._lhs_path(left)
            if lp and isinstance(self.env.get(lp[0]), _Struct):
                st: _Struct = self.env[lp[0]]
                fld = self._field(right)
                if op != "=" and lp[1] in st.fields:
                    prev = st.fields[lp[1]]
                    if prev.value is not None and fld.value is not None:
                        try:
                            fld = _Field(_binop(op[:-1], prev.value, fld.value, _txt(n)),
                                         None, fld.line, fld.text)
                        except _Unres:
                            fld = _Field(None, None, fld.line, fld.text)
                    else:
                        fld = _Field(None, None, fld.line, fld.text)
                st.fields[lp[1]] = fld
        self.exec(right)

    def _for(self, n: Node) -> None:
        ex = self.ex
        init = n.child_by_field_name("initializer")
        cond = n.child_by_field_name("condition")
        upd = n.child_by_field_name("update")
        body = n.child_by_field_name("body")
        var = start = None
        try:
            if init is not None:
                if init.type == "declaration":
                    d = init.child_by_field_name("declarator")
                    if d is not None and d.type == "init_declarator":
                        var = _txt(d.child_by_field_name("declarator"))
                        start = ex.ev(d.child_by_field_name("value"), self.file, self.env)
                elif init.type == "assignment_expression":
                    var = _txt(init.child_by_field_name("left"))
                    start = ex.ev(init.child_by_field_name("right"), self.file, self.env)
            if var is None or start is None or cond is None or cond.type != "binary_expression":
                raise _Unres("loop")
            if _txt(cond.child_by_field_name("left")) != var:
                raise _Unres("loop")
            cop = _txt(cond.child_by_field_name("operator"))
            bound = ex.ev(cond.child_by_field_name("right"), self.file, self.env)
            if cop not in ("<", "<="):
                raise _Unres("loop")
            if not re.fullmatch(rf"(\+\+\s*{re.escape(var)}|{re.escape(var)}\s*\+\+|{re.escape(var)}\s*\+=\s*1)", _txt(upd)):
                raise _Unres("loop")
            stop = bound if cop == "<" else bound + 1
            if stop - start > MAX_UNROLL:
                raise _Unres("loop too long")
        except _Unres:
            if var:
                self.env[var] = _Poison()
            self.exec(body)
            return
        saved = self.env.get(var)
        for i in range(start, stop):
            self.env[var] = i
            self.exec(body)
        if saved is None:
            self.env.pop(var, None)
        else:
            self.env[var] = saved

    # --- calls
    def _pin(self, arg: Node | None, call: Node, what: str) -> int | None:
        try:
            return self.ex.ev(arg, self.file, self.env)
        except _Unres:
            self.ex.unres(self.file, call, _txt(call), f"{what}: pin argument not resolvable statically")
            return None

    def _use(self, kind: str, node: Node, **kw: Any) -> Use:
        return Use(kind=kind, file=self.file, line=node.start_point[0] + 1, function=self.func, **kw)

    def _call(self, n: Node) -> None:
        fn = _txt(n.child_by_field_name("function"))
        args = n.child_by_field_name("arguments").named_children
        ex = self.ex
        if fn == "gpio_config" and args:
            a = args[0]
            if a.type == "pointer_expression":
                a = a.child_by_field_name("argument")
            st = self.env.get(_txt(a)) if a is not None else None
            if isinstance(st, _Struct) and st.type == "gpio_config_t":
                self._emit_gpio_config(st, n)
            else:
                ex.unres(self.file, n, _txt(n), "gpio_config: config struct not found in this function")
        elif fn == "gpio_set_direction" and len(args) >= 2:
            p = self._pin(args[0], n, fn)
            m = ex.sym(args[1], self.file)
            if p is not None:
                mode = MODES.get(m or "")
                if mode is None:
                    ex.unres(self.file, n, _txt(n), "gpio_set_direction: mode not resolvable")
                ex.add(p, self._use("set_direction", n, mode=mode))
        elif fn == "gpio_set_pull_mode" and len(args) >= 2:
            p = self._pin(args[0], n, fn)
            m = ex.sym(args[1], self.file)
            if p is not None:
                up_down = PULL_MODES.get(m or "")
                if up_down is None:
                    ex.unres(self.file, n, _txt(n), "gpio_set_pull_mode: mode not resolvable")
                    up_down = (None, None)
                ex.add(p, self._use("set_pull", n, pull_up=up_down[0], pull_down=up_down[1]))
        elif fn in ("gpio_pullup_en", "gpio_pullup_dis", "gpio_pulldown_en", "gpio_pulldown_dis") and args:
            p = self._pin(args[0], n, fn)
            if p is not None:
                on = fn.endswith("_en")
                if "pullup" in fn:
                    ex.add(p, self._use("pullup", n, pull_up=on))
                else:
                    ex.add(p, self._use("pulldown", n, pull_down=on))
        elif fn == "gpio_reset_pin" and args:
            p = self._pin(args[0], n, fn)
            if p is not None:
                ex.add(p, self._use("reset", n))
        elif fn == "uart_set_pin" and len(args) >= 5:
            for a, sig in zip(args[1:5], ("tx", "rx", "rts", "cts"), strict=True):
                try:
                    p = ex.ev(a, self.file, self.env)
                except _Unres:
                    ex.unres(self.file, n, _txt(n), f"uart_set_pin {sig}: pin not resolvable")
                    continue
                if p >= 0:
                    ex.add(p, self._use("driver", n, driver="uart", signal=sig))

    def _emit_gpio_config(self, st: _Struct, call: Node) -> None:
        ex = self.ex
        f = st.fields
        mask = f.get("pin_bit_mask")
        if mask is None:
            return
        if mask.value is None:
            ex.unres(self.file, mask.line or call, mask.text or _txt(call),
                     "gpio_config pin_bit_mask not resolvable statically")
            return
        mf = f.get("mode")
        mode = "disable"
        if mf is not None:
            mode = MODES.get(mf.sym or "")  # type: ignore[assignment]
            if mode is None:
                ex.unres(self.file, mf.line, mf.text, "gpio_config mode not resolvable")

        def flag(name: str) -> bool | None:
            x = f.get(name)
            if x is None:
                return False
            if x.value is not None:
                return bool(x.value)
            if x.sym and x.sym.endswith("_ENABLE"):
                return True
            if x.sym and x.sym.endswith("_DISABLE"):
                return False
            ex.unres(self.file, x.line, x.text, f"gpio_config {name} not resolvable")
            return None

        up, down = flag("pull_up_en"), flag("pull_down_en")
        intr = f["intr_type"].sym if "intr_type" in f else "GPIO_INTR_DISABLE"
        for bit in range(64):
            if mask.value >> bit & 1:
                ex.add(bit, self._use("gpio_config", call, mode=mode, pull_up=up,
                                      pull_down=down, intr=intr))
        if mask.value < 0:
            ex.unres(self.file, call, _txt(call), "negative pin_bit_mask")

    def finish(self) -> None:
        """Emit driver-config structs set up in this scope."""
        ex = self.ex
        for st in self.env.values():
            if not isinstance(st, _Struct) or st.type not in DRIVER_STRUCTS:
                continue
            for path, (driver, signal) in DRIVER_STRUCTS[st.type].items():
                fld = st.fields.get(path)
                if fld is None:
                    continue
                if fld.value is None:
                    ex.unres(self.file, fld.line, fld.text, f"{driver} {signal} pin not resolvable")
                    continue
                if fld.value < 0:
                    continue  # I2S_GPIO_UNUSED / -1: signal unused
                up = None
                for flag_path, pin_fields in PULLUP_FLAGS.get(st.type, {}).items():
                    if path in pin_fields:
                        ff = st.fields.get(flag_path)
                        up = bool(ff.value) if ff is not None and ff.value is not None else (
                            False if ff is None else None)
                ex.add(fld.value, Use(kind="driver", file=self.file, line=fld.line,
                                      function=self.func, driver=driver, signal=signal,
                                      pull_up=up))


def _source_files(target: Path, sources: list[str]) -> list[Path]:
    out: set[Path] = set()
    for s in sources:
        base = target / s
        if base.is_file() and base.suffix in (".c", ".h"):
            out.add(base)
            continue
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            rel = p.relative_to(target)
            if p.suffix in (".c", ".h") and p.is_file() and not SKIP_DIRS & set(rel.parts):
                out.add(p)
    return sorted(out)


def extract(target: Path, sources: list[str]) -> Extracted:
    """Recover the pin configuration the C code under `target/<sources>` sets up. Read-only."""
    target = Path(target).expanduser()
    return _Extractor(target).run(_source_files(target, sources))
