"""Pin rule checker: chip layer, then module layer (board layer is a hook).

Per-pin facts and message templates live in rules/chip/*.yaml and
rules/module/*.yaml. Logic that cannot be pure data lives here and refers to
rule IDs from those files. C16 is a codegen rule and is not checked here.
"""

from __future__ import annotations

from functools import cache
from importlib import resources
from typing import Any

import yaml

from cnlp.manifest import Finding, Manifest, Pin


@cache
def _load(layer: str, name: str) -> dict[str, Any]:
    res = resources.files("cnlp") / "rules" / layer / f"{name}.yaml"
    return yaml.safe_load(res.read_text())


class _Layer:
    def __init__(self, data: dict[str, Any]):
        self.sets = {k: v for k, v in data["pin_sets"].items()}
        self.rules = data["rules"]

    def finding(self, key: str, pin: Pin | None = None, gpio: int | None = None, **kw: Any) -> Finding:
        r = self.rules[key]
        g = pin.gpio if pin is not None else gpio
        text = r["message"].format(
            gpio=g, role=pin.role if pin else "", mode=pin.mode if pin else "", **kw
        )
        return Finding(rule=r.get("id", key), severity=r["severity"], message=text, gpio=g)


def _chip_layer(m: Manifest) -> list[Finding]:
    L = _Layer(_load("chip", m.chip))
    s = L.sets
    out: list[Finding] = []
    f = m.features
    any_adc = any(p.function == "adc" for p in m.pins)

    by_gpio: dict[int, list[Pin]] = {}
    for p in m.pins:
        by_gpio.setdefault(p.gpio, []).append(p)
    for g, ps in by_gpio.items():
        if len(ps) > 1:
            out.append(L.finding("OWNER", gpio=g, roles=" and ".join(p.role for p in ps)))

    for p in m.pins:
        g = p.gpio
        if g in s["not_bonded"]:
            out.append(L.finding("C1", p))
        if g in s["input_only"]:
            if p.mode != "input":
                out.append(L.finding("C2_OUTPUT", p))
            if p.pull in ("internal_up", "internal_down"):
                out.append(L.finding("C2_PULL", p))
        if g in s["flash"]:
            out.append(L.finding("C3", p))
        if g in s["strapping_warn"]:
            out.append(L.finding(s["strapping_warn"][g], p))
        if g in s["adc2"] and f.wifi:
            out.append(L.finding("C10" if p.function == "adc" else "C10_GPIO", p))
        if p.function == "interrupt" and g in s["adc_interrupt_errata"]:
            if any_adc:
                out.append(L.finding("C11", p, reason="while any ADC is in use"))
            elif (f.wifi or f.bluetooth) and f.deep_sleep:
                out.append(L.finding("C11", p, reason="with Wi-Fi/Bluetooth and deep sleep"))
        if g in s["uart0"] and f.console == "uart0":
            out.append(L.finding("C12", p))
        if g in s["jtag"] and f.jtag:
            out.append(L.finding("C13", p))
        if p.wake is not None and g not in s["rtc"]:
            out.append(L.finding("C14", p))
        if p.function == "dac" and g not in s["dac"]:
            out.append(L.finding("C15", p))
        if (p.mode == "open_drain" or p.function == "i2c") and p.pull == "none":
            out.append(L.finding("OPEN_DRAIN_PULL", p))
        if p.function == "i2c" and p.pull == "internal_up":
            out.append(L.finding("I2C_WEAK_PULL", p))
        if p.mode == "input" and p.function in ("gpio", "interrupt") and p.pull == "none":
            out.append(L.finding("FLOATING", p))
    return out


def _module_layer(m: Manifest) -> list[Finding]:
    L = _Layer(_load("module", m.module))
    s = L.sets
    out: list[Finding] = []
    for p in m.pins:
        g = p.gpio
        if g in s["flash"]:
            out.append(L.finding("W1", p))
        # W2: GPIO 16/17 are ordinary GPIOs here, so C4 produces nothing.
        if g in s["mtdi"]:
            if p.pull == "external_up":
                out.append(L.finding("W3_EXTERNAL_PULLUP", p))
            elif p.reset_level != "low":  # inputs and outputs alike
                out.append(L.finding("W3_RESET", p))
        if g in s["strapping"] and p.reset_level is None:
            out.append(L.finding("W4", p))
        if g in s["not_exposed"]:
            out.append(L.finding("W5", p))
    return out


def _board_layer(m: Manifest) -> list[Finding]:
    """Hook: board rules wait for a board description."""
    return []


def check(m: Manifest) -> list[Finding]:
    found = _chip_layer(m) + _module_layer(m) + _board_layer(m)
    return sorted(found, key=lambda x: (-1 if x.gpio is None else x.gpio, x.rule, x.message))
