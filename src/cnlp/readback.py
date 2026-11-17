"""Parse ESP-IDF v5.5 `gpio_dump_io_configuration()` output and diff it against the manifest.

Line formats come from components/esp_driver_gpio/src/gpio.c (v5.5.6):

    ================IO DUMP Start================
    IO[<n>] -                          (or `IO[<n>] **RESERVED** -`)
      Pullup: <0|1>, Pulldown: <0|1>, DriveCap: <n>
      InputEn: <0|1>, OutputEn: <0|1|[periph_sig_ctrl]>[ (inversed)], OpenDrain: <0|1>
      FuncSel: <n> (GPIO|IOMUX)
      GPIO Matrix SigOut ID: <n>[ (simple GPIO output)]     only when FuncSel is GPIO
      GPIO Matrix SigIn ID: <n> <n> ... | (simple GPIO input)   only when InputEn and FuncSel is GPIO
      SleepSelEn: <0|1>
    (blank line)
    =================IO DUMP End=================
"""

from __future__ import annotations

import re

from cnlp.manifest import Manifest, Pin

BEGIN = "=== CNLP READBACK BEGIN ==="
END = "=== CNLP READBACK END ==="

FUNC_GPIO = 2  # FuncSel value for GPIO on the classic ESP32 (PIN_FUNC_GPIO)
SIG_GPIO_OUT = 256  # SIG_GPIO_OUT_IDX: the GPIO matrix output is the plain GPIO register
NO_PULL_HW = range(34, 40)  # GPIO34-39 have no pull-up/pull-down hardware

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_START = re.compile(r"=+IO DUMP Start=+")
_STOP = re.compile(r"=+IO DUMP End=+")
_IO = re.compile(r"IO\[(\d+)\]( \*\*RESERVED\*\*)? -")
_PULL = re.compile(r"Pullup: ([01]), Pulldown: ([01]), DriveCap: (\d+)")
_DIR = re.compile(r"InputEn: ([01]), OutputEn: (0|1|\[periph_sig_ctrl\])( \(inversed\))?, OpenDrain: ([01])")
_FUNC = re.compile(r"FuncSel: (\d+) \((GPIO|IOMUX)\)")
_SIGOUT = re.compile(r"GPIO Matrix SigOut ID: (\d+)( \(simple GPIO output\))?")
_SIGIN = re.compile(r"GPIO Matrix SigIn ID:((?: \d+)+| \(simple GPIO input\))")
_SLEEP = re.compile(r"SleepSelEn: ([01])")


class ReadbackError(ValueError):
    """The serial log has no usable GPIO dump."""


def _clean(text: str) -> list[str]:
    return [_ANSI.sub("", ln).replace("\r", "").rstrip() for ln in text.split("\n")]


def _extract_block(lines: list[str]) -> list[str]:
    begins = [i for i, ln in enumerate(lines) if ln.strip() == BEGIN]
    if begins:
        start = begins[-1] + 1
        for j in range(start, len(lines)):
            if lines[j].strip() == END:
                return lines[start:j]
        raise ReadbackError(f"The log has '{BEGIN}' but no '{END}' after it; the capture was cut off.")
    starts = [i for i, ln in enumerate(lines) if _START.fullmatch(ln.strip())]
    if not starts:
        raise ReadbackError("No GPIO dump found in the log (no CNLP READBACK markers and no 'IO DUMP Start').")
    start = starts[-1]
    for j in range(start + 1, len(lines)):
        if _STOP.fullmatch(lines[j].strip()):
            return lines[start : j + 1]
    raise ReadbackError("The last 'IO DUMP Start' has no matching 'IO DUMP End'; the capture was cut off.")


def parse(text: str) -> dict[int, dict]:
    """Return {gpio: register facts} from the last dump in `text`."""
    block = _extract_block(_clean(text))
    regs: dict[int, dict] = {}
    cur: dict | None = None
    bad: list[str] = []
    for ln in block:
        s = ln.strip()
        if not s or _START.fullmatch(s) or _STOP.fullmatch(s):
            continue
        if m := _IO.fullmatch(s):
            cur = {
                "pullup": False, "pulldown": False, "input_en": False, "output_en": False,
                "open_drain": False, "func_sel": -1, "func_name": "", "sig_out": None,
                "sig_in": None, "drive_cap": 0, "sleep_sel": False,
                "reserved": bool(m.group(2)), "raw": [s],
            }
            regs[int(m.group(1))] = cur
            continue
        if cur is None:
            bad.append(s)
            continue
        cur["raw"].append(s)
        if m := _PULL.fullmatch(s):
            cur["pullup"], cur["pulldown"], cur["drive_cap"] = m.group(1) == "1", m.group(2) == "1", int(m.group(3))
        elif m := _DIR.fullmatch(s):
            cur["input_en"] = m.group(1) == "1"
            cur["output_en"] = None if m.group(2).startswith("[") else m.group(2) == "1"
            cur["open_drain"] = m.group(4) == "1"
        elif m := _FUNC.fullmatch(s):
            cur["func_sel"], cur["func_name"] = int(m.group(1)), m.group(2)
        elif m := _SIGOUT.fullmatch(s):
            cur["sig_out"] = int(m.group(1))
        elif m := _SIGIN.fullmatch(s):
            cur["sig_in"] = m.group(1).strip()
        elif m := _SLEEP.fullmatch(s):
            cur["sleep_sel"] = m.group(1) == "1"
        else:
            bad.append(s)
    if bad:
        shown = "; ".join(repr(b) for b in bad[:5])
        raise ReadbackError(f"The GPIO dump has {len(bad)} line(s) I can't parse: {shown}")
    if not regs:
        raise ReadbackError("The GPIO dump block is empty: it has no IO[n] entries.")
    for n, r in regs.items():
        if r["func_sel"] < 0:
            raise ReadbackError(f"IO[{n}] in the dump has no FuncSel line.")
    return regs


def _is_plain_gpio(pin: Pin) -> bool:
    return pin.function in ("gpio", "interrupt")


def checked_fields(pin: Pin) -> list[str]:
    """Which register fields `diff` verifies for this pin (for the evidence matrix)."""
    fields = ["pullup", "pulldown"]
    if _is_plain_gpio(pin):
        if pin.mode == "output":
            fields += ["func_sel", "sig_out", "input_en", "open_drain"]
        elif pin.mode == "open_drain":
            fields += ["func_sel", "sig_out", "open_drain"]
        elif pin.mode == "input":
            fields += ["func_sel", "input_en", "output_en", "open_drain"]
    # MVP limit: adc/dac/pwm/bus pins are configured by their drivers (IOMUX or
    # matrix routing varies), so only the pulls are checked for them.
    return sorted(fields)


def expected(pin: Pin) -> dict:
    """Register facts the manifest implies for this pin (only the fields in checked_fields)."""
    up = pin.pull == "internal_up" and pin.gpio not in NO_PULL_HW
    down = pin.pull == "internal_down" and pin.gpio not in NO_PULL_HW
    exp: dict = {"pullup": up, "pulldown": down}
    if _is_plain_gpio(pin):
        if pin.mode in ("output", "open_drain"):
            # OutputEn is deliberately not checked: a GPIO-matrix output prints
            # `[periph_sig_ctrl]`. gpio_ll_get_io_config sets oe_ctrl_by_periph = !OEN_SEL, and
            # gpio.c prints that marker whenever it is set (or FuncSel is not GPIO), so the
            # `OutputEn: 1` bit never appears for a normal output on the real hardware.
            # Instead the evidence is FuncSel == GPIO plus SigOut ID == 256 (SIG_GPIO_OUT_IDX,
            # "simple GPIO output": the pad is driven by the GPIO_OUT register, not a peripheral).
            exp.update(func_sel=FUNC_GPIO, sig_out=SIG_GPIO_OUT, open_drain=pin.mode == "open_drain")
            if pin.mode == "output":
                exp["input_en"] = False
        elif pin.mode == "input":
            exp.update(func_sel=FUNC_GPIO, input_en=True, output_en=False, open_drain=False)
    return exp


_LABEL = {
    "pullup": "pull-up",
    "pulldown": "pull-down",
    "input_en": "input buffer",
    "output_en": "output enable",
    "open_drain": "open-drain mode",
    "func_sel": "pin function select (2 = GPIO, 0 = IOMUX)",
    "sig_out": "output signal ID (256 = plain GPIO output)",
}


def _message(pin: Pin, field: str, exp, act) -> str:
    who = f"GPIO {pin.gpio} ({pin.role})"
    label = _LABEL[field]
    if isinstance(exp, bool):
        want = "on" if exp else "off"
        got = "on" if act else "off"
        return f"{who}: {label} should be {want} per the manifest, but the register shows it {got}."
    return f"{who}: {label} should be {exp} per the manifest, but the register shows {act}."


def diff(m: Manifest, regs: dict[int, dict]) -> list[dict]:
    """Return one mismatch dict per field where the register disagrees with the manifest."""
    out: list[dict] = []
    for pin in m.pins:
        r = regs.get(pin.gpio)
        if r is None:
            out.append({
                "gpio": pin.gpio, "role": pin.role, "field": "present", "expected": True, "actual": False,
                "message": f"GPIO {pin.gpio} ({pin.role}): the manifest uses this pin, but it is missing from the register dump.",
            })
            continue
        for field, exp in expected(pin).items():
            act = r.get(field)
            if act != exp:
                out.append({
                    "gpio": pin.gpio, "role": pin.role, "field": field, "expected": exp, "actual": act,
                    "message": _message(pin, field, exp, act),
                })
    out.sort(key=lambda d: (d["gpio"], d["field"]))
    return out
