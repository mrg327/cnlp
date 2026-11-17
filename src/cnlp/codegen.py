"""Generate the ESP-IDF `board` component from a Manifest."""

from __future__ import annotations

import hashlib
import re
from importlib import resources
from pathlib import Path

import jinja2

from cnlp.manifest import Manifest, Pin, StateMachine, Transition


class CodegenError(ValueError):
    """Raised when the manifest cannot be turned into code."""


_TEMPLATES = ["CMakeLists.txt", "board_pins.c", "board_drivers.c",
              "board_pins.h", "board_drivers.h"]
_OUT_PATH = {"board_pins.h": "include/board_pins.h", "board_drivers.h": "include/board_drivers.h"}
_BUS_RE = re.compile(r"^i2c(\d+)_(sda|scl)$")


def _env() -> jinja2.Environment:
    tdir = resources.files("cnlp").joinpath("templates")
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(tdir)),
        trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined, autoescape=False,
    )


def _define(p: Pin) -> str:
    return "PIN_" + p.role.upper()


def _gpio_mode(p: Pin) -> str:
    return {"input": "GPIO_MODE_INPUT", "output": "GPIO_MODE_OUTPUT",
            "open_drain": "GPIO_MODE_OUTPUT_OD"}[p.mode]


def _buses(pins: list[Pin]) -> list[dict]:
    found: dict[int, dict[str, Pin]] = {}
    for p in pins:
        m = _BUS_RE.match(p.bus or "")
        if m:
            found.setdefault(int(m.group(1)), {})[m.group(2)] = p
    out = []
    for n in sorted(found):
        b = found[n]
        if "sda" in b and "scl" in b:
            out.append({"n": n, "sda": _define(b["sda"]), "scl": _define(b["scl"]),
                        "pullup": b["sda"].pull == "internal_up" and b["scl"].pull == "internal_up"})
        else:
            have = "sda" if "sda" in b else "scl"
            miss = "scl" if have == "sda" else "sda"
            raise CodegenError(f"I2C bus i2c{n} has an {have} pin but no {miss} pin; "
                               f"add a pin with bus i2c{n}_{miss}")
    return out


def _dur(ms: int) -> str:
    for unit, k in (("min", 60000), ("s", 1000)):
        if ms % k == 0:
            return f"{ms // k} {unit}"
    return f"{ms} ms"


def _machine_ctx(sm: StateMachine) -> dict:
    n = sm.name.upper()
    st_enum = {s.name: f"{n}_STATE_{s.name.upper()}" for s in sm.states}
    events: list[str] = []
    actions: list[str] = []
    for s in sm.states:
        for t in s.transitions:
            if t.kind == "every":
                if t.action not in actions:
                    actions.append(t.action or "")
            elif t.event not in events:
                events.append(t.event or "")
    events.sort()
    actions.sort()

    def text(t: Transition) -> str:
        ev = (t.event or "").replace("_", " ")
        if t.kind == "after":
            return f"after {t.count} {ev} go to {t.target}"
        return f"on {ev} go to {t.target}"

    rows, periodic = [], []
    for s in sm.states:
        for t in s.transitions:
            if t.kind == "every":
                periodic.append({
                    "state": st_enum[s.name], "period_ms": t.period_ms,
                    "action": f"{n}_ACTION_{(t.action or '').upper()}",
                    "text": f"{s.name}: every {_dur(t.period_ms or 0)} do {(t.action or '').replace('_', ' ')}",
                })
            else:
                rows.append({
                    "state": st_enum[s.name], "event": f"{n}_EVENT_{(t.event or '').upper()}",
                    "next": st_enum[t.target or ""], "threshold": t.count or 0,
                    "text": f"{s.name}: {text(t)}",
                })
    return {
        "name": sm.name, "NAME": n, "start": sm.start, "start_enum": st_enum[sm.start],
        "states": [{"name": s.name, "enum": st_enum[s.name]} for s in sm.states],
        "events": [{"name": e, "enum": f"{n}_EVENT_{e.upper()}"} for e in events],
        "actions": [{"name": a, "enum": f"{n}_ACTION_{a.upper()}"} for a in actions],
        "rows": rows, "periodic": periodic,
    }


def _check_machines(machines: tuple[StateMachine, ...]) -> None:
    """Refuse a machine the table generator can't express; `cnlp check-fsm` explains more."""
    names = [sm.name for sm in machines]
    if len(set(names)) != len(names):
        raise CodegenError("Two state machines share a name; give each its own name.")
    for sm in machines:
        sts = [s.name for s in sm.states]
        if len(set(sts)) != len(sts):
            raise CodegenError(f"Machine '{sm.name}' declares a state twice; run `cnlp check-fsm`.")
        if sm.start not in sts:
            raise CodegenError(f"Machine '{sm.name}' starts in '{sm.start}', which is not declared; "
                               "run `cnlp check-fsm`.")
        for s in sm.states:
            for t in s.transitions:
                if t.kind != "every" and t.target not in sts:
                    raise CodegenError(f"In machine '{sm.name}', state '{s.name}' goes to the "
                                       f"undeclared state '{t.target}'; run `cnlp check-fsm`.")


def generate(m: Manifest, out_dir: Path) -> list[dict]:
    out_dir = Path(out_dir)
    pins = sorted(m.pins, key=lambda p: (p.gpio, p.role))
    buses = _buses(pins)
    rows = []
    for p in pins:
        up = p.pull == "internal_up"
        down = p.pull == "internal_down"
        rows.append({
            "gpio": p.gpio, "define": _define(p), "mode": p.mode, "pull": p.pull,
            "function": p.function, "gpio_mode": _gpio_mode(p),
            "output": p.mode in ("output", "open_drain"),
            "pull_up": "GPIO_PULLUP_ENABLE" if up else "GPIO_PULLUP_DISABLE",
            "pull_down": "GPIO_PULLDOWN_ENABLE" if down else "GPIO_PULLDOWN_DISABLE",
            "intr": "GPIO_INTR_ANYEDGE" if p.function == "interrupt" else "GPIO_INTR_DISABLE",
        })
    _check_machines(m.state_machines)
    fsm_ctx = [_machine_ctx(sm) for sm in m.state_machines]
    srcs = ['"board_pins.c"', '"board_drivers.c"'] + [f'"fsm_{c["name"]}.c"' for c in fsm_ctx]
    mask_expr = "|".join(f"(1ULL<<{p.gpio})" for p in pins) or "0ULL"
    ctx = {
        "manifest_sha": hashlib.sha256(m.to_yaml().encode()).hexdigest(),
        "pins": rows,
        "gpio_pins": [r for r in rows if r["function"] in ("gpio", "interrupt")],
        "mask_expr": mask_expr,
        "buses": buses,
        "srcs": " ".join(srcs),
        "requires": ["esp_driver_gpio"] + (["esp_driver_i2c"] if buses else []),
    }
    env = _env()
    result = []
    for name in _TEMPLATES:
        rel = _OUT_PATH.get(name, name)
        data = env.get_template(name + ".j2").render(**ctx).encode()
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        result.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest()})
    for c in fsm_ctx:
        for tmpl, rel in (("fsm.h", f"include/fsm_{c['name']}.h"), ("fsm.c", f"fsm_{c['name']}.c")):
            data = env.get_template(tmpl + ".j2").render(m=c, manifest_sha=ctx["manifest_sha"]).encode()
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            result.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest()})
    return sorted(result, key=lambda d: d["path"])
