"""Pin rule checker: chip layer, then module layer (board layer is a hook).

Per-pin facts and message templates live in rules/chip/*.yaml and
rules/module/*.yaml. Logic that cannot be pure data lives here and refers to
rule IDs from those files. C16 is a codegen rule and is not checked here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import cache
from importlib import resources
from typing import Any

import yaml

from cnlp.manifest import Finding, Manifest, Pin


@cache
def _load(layer: str, name: str) -> dict[str, Any]:
    res = resources.files("cnlp") / "rules" / layer / f"{name}.yaml"
    return yaml.safe_load(res.read_text())


Suggest = Callable[[Pin], str]


def _no_suggest(_pin: Pin) -> str:
    return ""


class _Layer:
    def __init__(self, data: dict[str, Any], suggest: Suggest = _no_suggest):
        self.sets = {k: v for k, v in data["pin_sets"].items()}
        self.rules = data["rules"]
        self.suggest = suggest

    def finding(self, key: str, pin: Pin | None = None, gpio: int | None = None,
                move: Pin | None = None, **kw: Any) -> Finding:
        """`move` is the pin a {suggest} placeholder should find a new home for (default: pin)."""
        r = self.rules[key]
        g = pin.gpio if pin is not None else gpio
        msg = r["message"]
        if "{suggest}" in msg:
            target = move or pin
            kw["suggest"] = self.suggest(target) if target else ""
        text = msg.format(
            gpio=g, role=pin.role if pin else "", mode=pin.mode if pin else "", **kw
        ).rstrip()
        return Finding(rule=r.get("id", key), severity=r["severity"], message=text, gpio=g)


def _chip_layer(m: Manifest, suggest: Suggest = _no_suggest) -> list[Finding]:
    L = _Layer(_load("chip", m.chip), suggest)
    s = L.sets
    out: list[Finding] = []
    f = m.features
    any_adc = any(p.function == "adc" for p in m.pins)

    by_gpio: dict[int, list[Pin]] = {}
    for p in m.pins:
        by_gpio.setdefault(p.gpio, []).append(p)
    for g, ps in by_gpio.items():
        if len(ps) > 1:
            out.append(L.finding("OWNER", gpio=g, move=ps[-1],
                                 roles=" and ".join(p.role for p in ps)))

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
        if p.function == "adc" and g not in s["adc1"] and g not in s["adc2"]:
            out.append(L.finding("C17", p))
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


def _module_layer(m: Manifest, suggest: Suggest = _no_suggest) -> list[Finding]:
    L = _Layer(_load("module", m.module), suggest)
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


def _raw(m: Manifest, suggest: Suggest = _no_suggest) -> list[Finding]:
    found = _chip_layer(m, suggest) + _module_layer(m, suggest) + _board_layer(m)
    return sorted(found, key=lambda x: (-1 if x.gpio is None else x.gpio, x.rule, x.message))


MAX_SUGGESTIONS = 3


def free_pins(m: Manifest, pin: Pin, limit: int = MAX_SUGGESTIONS) -> list[int]:
    """GPIOs this pin could move to with no findings at all, given the rest of the plan.

    Every candidate is checked by moving the pin there and rerunning all rules, so rule
    constraints (input-only, RTC, ADC1 with Wi-Fi, DAC, pins already in use, strapping
    pins...) apply without special cases. A declared reset level does not move with it.
    """
    clean, _ = _candidates(m, pin, limit)
    return clean


def _candidates(m: Manifest, pin: Pin, limit: int) -> tuple[list[int], list[int]]:
    """(pins with no findings, pins with warnings only), each up to `limit`."""
    others = [p for p in m.pins if p is not pin]
    clean: list[int] = []
    warned: list[int] = []
    for g in range(40):
        if g == pin.gpio:
            continue
        moved = replace(pin, gpio=g, reset_level=None)
        here = [f for f in _raw(replace(m, pins=(*others, moved))) if f.gpio == g]
        if not here:
            clean.append(g)
        elif all(f.severity == "warning" for f in here) and len(warned) < limit:
            warned.append(g)
        if len(clean) == limit:
            break
    return clean, warned


def _suggester(m: Manifest) -> Suggest:
    def names(pins: list[int]) -> str:
        return ", ".join(f"GPIO {g}" for g in pins)

    def suggest(pin: Pin) -> str:
        clean, warned = _candidates(m, pin, MAX_SUGGESTIONS)
        if clean:
            return f"Free pins that pass every rule for {pin.role}: {names(clean)}."
        if warned:
            return (f"No free pin is warning-free for {pin.role}; these work with a warning "
                    f"(check with cnlp try): {names(warned)}.")
        return f"No free pin in this plan passes every rule for {pin.role}; free one up first."
    return suggest


def check(m: Manifest) -> list[Finding]:
    """All findings. Messages that need a new pin name free pins checked against the whole plan."""
    return _raw(m, _suggester(m))
