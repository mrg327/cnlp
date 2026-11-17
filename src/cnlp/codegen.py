"""Generate the ESP-IDF `board` component from a Manifest."""

from __future__ import annotations

import hashlib
import re
from importlib import resources
from pathlib import Path

import jinja2

from cnlp.manifest import Manifest, Pin


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
    mask_expr = "|".join(f"(1ULL<<{p.gpio})" for p in pins) or "0ULL"
    ctx = {
        "manifest_sha": hashlib.sha256(m.to_yaml().encode()).hexdigest(),
        "pins": rows,
        "gpio_pins": [r for r in rows if r["function"] in ("gpio", "interrupt")],
        "mask_expr": mask_expr,
        "buses": buses,
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
    return sorted(result, key=lambda d: d["path"])
