"""Key-parameter audit: expected values (CLAUDE.md "Key parameters") vs the project snapshot.

`check(root, sources, spec)` works on a snapshot dir only (`audit.py` copies the target).
`spec` is a parsed `parameters.yaml`::

    source: "CLAUDE.md Key parameters"   # quoted in messages
    sdkconfig: {CONFIG_FREERTOS_HZ: 1000}            # root/sdkconfig (then sdkconfig.defaults)
    constants: {AUDIO_SAMPLE_RATE_HZ: 48000}         # #define / const / enum, resolved like extract
    driver_fields: {i2s_chan_config_t.dma_desc_num: 2,
                    i2s_std_config_t.clk_cfg.sample_rate_hz: 48000}
    tasks: {audio_task: {priority: 20, core: 1}}     # key = task function or task name string
    derived: {buffer_period_ms: {frames: AUDIO_FRAMES_PER_BUF, rate: AUDIO_SAMPLE_RATE_HZ}}

Rules: PARAM_SDKCONFIG, PARAM_CONSTANT, PARAM_DRIVER_FIELD, PARAM_TASK (errors on a
differing value); PARAM_MISSING / PARAM_TASK_MISSING (warnings: "not defined / created
yet", expected mid-project); PARAM_UNRESOLVED (warning: the value can't be determined
statically; never guessed). Driver fields come from local struct variables in function
bodies (initializers, `cfg.field = x`, `I2S_STD_CLK_DEFAULT_CONFIG(rate)` for clk_cfg);
tasks from xTaskCreate / xTaskCreatePinnedToCore / ...Static calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cnlp import extract as X
from cnlp.manifest import Finding

NO_AFFINITY = 0x7FFFFFFF
BUILTINS = {"tskNO_AFFINITY": str(NO_AFFINITY), "configMAX_PRIORITIES": "25"}
# ESP-IDF v5.x defaults, for keys absent from sdkconfig.
SDK_DEFAULTS = {
    "CONFIG_FREERTOS_HZ": "100",
    "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ": "160",
    "CONFIG_FREERTOS_UNICORE": "n",
    "CONFIG_ESP_TASK_WDT_TIMEOUT_S": "5",
}
TASK_FNS = {"xTaskCreate": False, "xTaskCreatePinnedToCore": True,
            "xTaskCreateStatic": False, "xTaskCreateStaticPinnedToCore": True}
CLK_MACRO = re.compile(r"I2S_(?:STD|TDM)_CLK_DEFAULT_CONFIG\s*\((.*)\)\s*$", re.DOTALL)


@dataclass
class TaskSite:
    fn: str
    name: str
    file: str
    line: int
    priority: int | None
    core: int | None
    prio_text: str
    core_text: str


@dataclass
class ParamResult:
    findings: list[Finding] = field(default_factory=list)
    derived: dict[str, float] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)


class _ParamScope(X._Scope):
    def __init__(self, ex: X._Extractor, file: str, func: str, col: _Collector) -> None:
        super().__init__(ex, file, func)
        self.col = col

    def _decl(self, n: Any) -> None:
        type_name = X._txt(n.child_by_field_name("type"))
        if type_name in self.col.struct_types:
            for d in n.children_by_field_name("declarator"):
                decl = d.child_by_field_name("declarator") if d.type == "init_declarator" else d
                val = d.child_by_field_name("value") if d.type == "init_declarator" else None
                if decl is not None and decl.type == "identifier":
                    st = self._struct_from_decl(type_name, val, n.start_point[0] + 1)
                    self.env[X._txt(decl)] = st
                    self.col.structs.append((self, st))
        else:
            super()._decl(n)
        for c in X._find(n, "call_expression"):
            self._call(c)

    def _call(self, n: Any) -> None:
        fn = X._txt(n.child_by_field_name("function"))
        if fn not in TASK_FNS or n.start_byte in self.col.seen:
            return
        self.col.seen.add(n.start_byte)
        args = n.child_by_field_name("arguments").named_children
        pinned = TASK_FNS[fn]
        if len(args) < (7 if pinned else 6):
            return
        name = X._txt(args[1]).strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        else:
            name = ""
        prio = self._try(args[4])
        core = self._try(args[6]) if pinned else NO_AFFINITY
        self.col.tasks.append(TaskSite(
            X._txt(args[0]), name, self.file, n.start_point[0] + 1, prio, core,
            " ".join(X._txt(args[4]).split()),
            " ".join(X._txt(args[6]).split()) if pinned else "tskNO_AFFINITY"))

    def _try(self, node: Any) -> int | None:
        try:
            return self.ex.ev(node, self.file, self.env)
        except X._Unres:
            return None


class _Collector:
    def __init__(self, struct_types: set[str]) -> None:
        self.struct_types = struct_types
        self.structs: list[tuple[_ParamScope, X._Struct]] = []
        self.tasks: list[TaskSite] = []
        self.seen: set[int] = set()


def _analyze(root: Path, sources: list[str], struct_types: set[str]):
    ex = X._Extractor(root)
    col = _Collector(struct_types)
    files = X._source_files(root, sources)
    parsed, decls = [], {}
    srcs: dict[str, list[str]] = {}
    for p in files:
        rel = p.relative_to(root).as_posix()
        data = p.read_bytes()
        tree = X._PARSER.parse(data)
        ex.keep.append(tree)
        parsed.append((rel, tree.root_node))
        srcs[rel] = data.decode("utf-8", "replace").splitlines()
    for rel, node in parsed:
        decls[rel] = ex.collect(rel, node, b"")
    for name, val in BUILTINS.items():
        ex.defs.put("macros", "<builtin>", name, val)
    scopes: list[_ParamScope] = []
    for rel, node in parsed:
        for fn in X._find(node, "function_definition"):
            fname = X._txt(X._inner_ident(fn.child_by_field_name("declarator")))
            sc = _ParamScope(ex, rel, fname, col)
            sc.exec(fn.child_by_field_name("body"))
            scopes.append(sc)
        top = _ParamScope(ex, rel, "", col)
        for d in decls[rel]:
            top.exec(d)
        scopes.append(top)
    return ex, col, srcs


def _def_site(srcs: dict[str, list[str]], name: str) -> tuple[str, int] | None:
    pat = re.compile(rf"^\s*(?:#\s*define\s+{re.escape(name)}\b|.*\b{re.escape(name)}\s*=[^=])")
    for rel in sorted(srcs):
        for i, line in enumerate(srcs[rel], 1):
            if pat.match(line):
                return rel, i
    return None


def parse_sdkconfig(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text("utf-8", "replace").splitlines():
        m = re.match(r"\s*(CONFIG_\w+)=(.*)$", line)
        if m:
            v = m.group(2).strip()
            out[m.group(1)] = v[1:-1] if len(v) > 1 and v[0] == v[-1] == '"' else v
            continue
        m = re.match(r"\s*#\s*(CONFIG_\w+) is not set", line)
        if m:
            out[m.group(1)] = "n"
    return out


def _norm(v: Any) -> str:
    if v is True:
        return "y"
    if v is False:
        return "n"
    return str(v).strip()


def _check_sdkconfig(root: Path, want: dict[str, Any], src: str) -> list[Finding]:
    out: list[Finding] = []
    main, defaults = root / "sdkconfig", root / "sdkconfig.defaults"
    cfg = parse_sdkconfig(main) if main.is_file() else None
    dfl = parse_sdkconfig(defaults) if defaults.is_file() else None
    if cfg is None and dfl is None:
        return [Finding("PARAM_SDKCONFIG", "warning",
                        "No sdkconfig or sdkconfig.defaults in the project; "
                        f"{len(want)} expected setting(s) not checked.", None)]
    for key, exp in want.items():
        e = _norm(exp)
        in_file = "sdkconfig" if cfg is not None and key in cfg else (
            "sdkconfig.defaults" if dfl is not None and key in dfl else None)
        if in_file is None:
            d = SDK_DEFAULTS.get(key)
            if d is not None and d == e:
                continue
            tail = f"; ESP-IDF default applies: {d}" if d is not None else ""
            out.append(Finding(
                "PARAM_SDKCONFIG", "warning",
                f"sdkconfig: {key} is not set{tail}. Expected {e} ({src}).", None))
            continue
        actual = (cfg if in_file == "sdkconfig" else dfl)[key]  # type: ignore[index]
        if actual != e:
            out.append(Finding(
                "PARAM_SDKCONFIG", "error",
                f"{in_file}: {key} is {actual}, expected {e} ({src}).", None))
        elif in_file == "sdkconfig" and dfl is not None and key in dfl and dfl[key] != e:
            out.append(Finding(
                "PARAM_SDKCONFIG", "warning",
                f"sdkconfig.defaults: {key} is {dfl[key]} but sdkconfig has {e}; "
                "a fullclean/regenerate would change it.", None))
    return out


def _check_constants(ex: X._Extractor, srcs, want: dict[str, Any], src: str,
                     actual: dict[str, int]) -> list[Finding]:
    out: list[Finding] = []
    for name, exp in want.items():
        site = _def_site(srcs, name)
        loc = f"{site[0]}:{site[1]}: " if site else ""
        if site is None and all(ex.defs.get(t, "", name) is None
                                for t in ("macros", "consts", "enums")):
            out.append(Finding("PARAM_MISSING", "warning",
                               f"{name} is not defined in the code yet (expected {exp}, {src}).",
                               None))
            continue
        try:
            v = ex._ident(name, "", {})
        except X._Unres:
            out.append(Finding("PARAM_UNRESOLVED", "warning",
                               f"{loc}{name} can't be resolved statically (expected {exp}, "
                               f"{src}). Not guessed; check by hand.", None))
            continue
        actual[name] = v
        if v != exp:
            out.append(Finding("PARAM_CONSTANT", "error",
                               f"{loc}{name} is {v}, expected {exp} ({src}).", None))
    return out


def _check_fields(col: _Collector, want: dict[str, Any], src: str) -> list[Finding]:
    found: dict[str, list[tuple[str, int, int | None, str]]] = {}
    for sc, st in col.structs:
        for path, fld in list(st.fields.items()):
            items = [(path, fld.value, fld.text, fld.line)]
            m = CLK_MACRO.search(fld.text)
            if m:
                try:
                    v: int | None = sc.ex.ev(sc.ex._macro_node(m.group(1).strip()), sc.file, sc.env)
                except X._Unres:
                    v = None
                items.append((path + ".sample_rate_hz", v, m.group(1).strip(), fld.line))
            for p, v, text, line in items:
                found.setdefault(f"{st.type}.{p}", []).append((sc.file, line, v, text))
    out: list[Finding] = []
    for key, exp in want.items():
        sites = found.get(key)
        if not sites:
            out.append(Finding("PARAM_MISSING", "warning",
                               f"{key} is not set in the code yet (expected {exp}, {src}).", None))
            continue
        for file, line, v, text in sites:
            if v is None:
                out.append(Finding("PARAM_UNRESOLVED", "warning",
                                   f"{file}:{line}: {key} = `{text}` can't be resolved "
                                   f"statically (expected {exp}, {src}). Check by hand.", None))
            elif v != exp:
                out.append(Finding("PARAM_DRIVER_FIELD", "error",
                                   f"{file}:{line}: {key} is {v}, expected {exp} ({src}).", None))
    return out


def _core_str(v: int | None, text: str) -> str:
    if v == NO_AFFINITY:
        return "no affinity (tskNO_AFFINITY)"
    return str(v) if v is not None else text


def _check_tasks(col: _Collector, want: dict[str, Any], src: str) -> tuple[list[Finding], list[dict]]:
    out: list[Finding] = []
    info: list[dict] = []
    for key, exp in want.items():
        sites = [t for t in col.tasks if key in (t.fn, t.name)]
        if not sites:
            out.append(Finding("PARAM_TASK_MISSING", "warning",
                               f"Task {key} is not created in the code yet (expected "
                               f"{_exp_str(exp)}, {src}).", None))
            continue
        for t in sites:
            loc = f"{t.file}:{t.line}: "
            info.append({"task": key, "file": t.file, "line": t.line,
                         "priority": t.priority, "core": t.core})
            for what, actual, text in (("priority", t.priority, t.prio_text),
                                       ("core", t.core, t.core_text)):
                if what not in exp:
                    continue
                if actual is None:
                    out.append(Finding("PARAM_UNRESOLVED", "warning",
                                       f"{loc}task {key} {what} `{text}` can't be resolved "
                                       f"statically (expected {exp[what]}, {src}).", None))
                elif actual != exp[what]:
                    shown = _core_str(actual, text) if what == "core" else str(actual)
                    out.append(Finding("PARAM_TASK", "error",
                                       f"{loc}task {key} is created with {what} {shown}, "
                                       f"expected {exp[what]} ({src}: {_exp_str(exp)}).", None))
    return out, info


def _exp_str(exp: dict[str, Any]) -> str:
    return ", ".join(f"{k} {v}" for k, v in exp.items())


def check(root: Path, sources: list[str], spec: dict[str, Any]) -> ParamResult:
    src = str(spec.get("source") or "parameters.yaml")
    fields_w = dict(spec.get("driver_fields") or {})
    types = {k.split(".", 1)[0] for k in fields_w}
    ex, col, srcs = _analyze(root, sources, types)
    res = ParamResult()
    f = res.findings
    if spec.get("sdkconfig"):
        f += _check_sdkconfig(root, dict(spec["sdkconfig"]), src)
    actual: dict[str, int] = {}
    consts = dict(spec.get("constants") or {})
    f += _check_constants(ex, srcs, consts, src, actual)
    f += _check_fields(col, fields_w, src)
    tf, res.tasks = _check_tasks(col, dict(spec.get("tasks") or {}), src)
    f += tf
    d = (spec.get("derived") or {}).get("buffer_period_ms")
    if d:
        def val(name: str) -> int | None:
            if name in actual:
                return actual[name]
            try:
                return ex._ident(name, "", {})
            except X._Unres:
                return consts.get(name)
        frames, rate = val(str(d["frames"])), val(str(d["rate"]))
        if frames and rate:
            res.derived["buffer_period_ms"] = round(frames / rate * 1000, 3)
    return res
