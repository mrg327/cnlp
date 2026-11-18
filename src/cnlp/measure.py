"""Take a measurement from the bench oscilloscope over SCPI (VISA), optionally as evidence.

Nothing here talks to an instrument unless `connect()` is called; every other function takes a
`scope` object exposing `query(str) -> str`, `write(str)` and `idn`, so tests use fakes.
Discovery mirrors the synth's tools/scope/scope.py: `SCOPE_RESOURCE`, else `SCOPE_HOST`
(default 192.168.50.2) with VXI-11, HiSLIP, then raw sockets 5025/5555/4000/3000.

Statistics are computed here by sampling the single-shot measurement query N times. The
scopes' built-in statistics (Rigol :MEASure:STATistic, Keysight/Siglent accumulators) need
per-vendor enabling and accumulation state that cannot be verified without hardware, so they
are deliberately not used; sampling works the same on every vendor.

SCPI per vendor is written from the programming manuals as remembered, NOT checked on the
instruments. Each table notes where we are unsure. `--raw-query` is the escape hatch.
"""

from __future__ import annotations

import math
import os
import re
import statistics
import time
from typing import Any

INSTALL_HINT = ('Install the scope extra: uv tool install --editable '
                '"$HOME/projects/cnlp[scope]" --force')
KINDS = ("period", "frequency", "pwidth", "nwidth", "duty", "delay")
KIND_UNIT = {"period": "s", "frequency": "Hz", "pwidth": "s", "nwidth": "s", "duty": "%",
             "delay": "s"}
KIND_LABEL = {"period": "Period", "frequency": "Frequency", "pwidth": "+Width",
              "nwidth": "-Width", "duty": "Duty", "delay": "Delay"}
INVALID = 9.0e37  # scopes report 9.9E37 (Tektronix 9.91E37) for "no valid measurement"


class MeasureError(Exception):
    pass


# --- connection -------------------------------------------------------------------------


def candidates(host: str | None = None) -> list[str]:
    host = host or os.environ.get("SCOPE_HOST", "192.168.50.2")
    return [f"TCPIP0::{host}::INSTR", f"TCPIP0::{host}::hislip0::INSTR"] + [
        f"TCPIP0::{host}::{port}::SOCKET" for port in (5025, 5555, 4000, 3000)]


class Scope:
    """Thin wrapper over an open VISA resource."""

    def __init__(self, res: Any, resource: str, idn: str) -> None:
        self._res, self.resource, self.idn = res, resource, idn

    def query(self, cmd: str) -> str:
        return str(self._res.query(cmd)).strip()

    def write(self, cmd: str) -> None:
        self._res.write(cmd)

    def close(self) -> None:
        self._res.close()


def _open(rm: Any, resource: str) -> Any:
    res = rm.open_resource(resource, open_timeout=2000)
    res.timeout = 3000  # ms
    if resource.endswith("SOCKET"):
        res.read_termination = "\n"
        res.write_termination = "\n"
    return res


def connect(resource: str | None = None, rm: Any = None) -> Scope:
    """Open the first resource that answers *IDN?. `rm` is injectable for tests."""
    if rm is None:
        try:
            import pyvisa
        except ImportError as e:
            raise MeasureError(INSTALL_HINT) from e
        rm = pyvisa.ResourceManager("@py")
    wanted = resource or os.environ.get("SCOPE_RESOURCE")
    tried = []
    for res_name in [wanted] if wanted else candidates():
        try:
            res = _open(rm, res_name)
            idn = str(res.query("*IDN?")).strip()
            return Scope(res, res_name, idn)
        except Exception as e:  # noqa: BLE001  unreachable, refused or timed out
            tried.append(f"{res_name} ({type(e).__name__})")
    raise MeasureError("No instrument answered; tried " + ", ".join(tried)
                       + ". Is this PC on the scope's subnet? Set SCOPE_RESOURCE or SCOPE_HOST.")


# --- vendor dialects --------------------------------------------------------------------

VENDORS = ("rigol", "siglent", "keysight", "tektronix")


def detect_vendor(idn: str) -> str:
    s = idn.lower()
    if "rigol" in s:
        return "rigol"
    if "siglent" in s:
        return "siglent"
    if "keysight" in s or "agilent" in s:
        return "keysight"
    if "tektronix" in s:
        return "tektronix"
    raise MeasureError(
        f"unsupported instrument {idn!r}; supported vendors: Rigol, Siglent, Keysight/Agilent, "
        "Tektronix. Use --raw-query '<SCPI query>' to send your own command.")


# Rigol DS1000Z/DS2000/MSO5000: `:MEASure:ITEM? <item>,CHANnel<n>[,CHANnel<m>]`.
# Unsure: delay with two sources on DS1000Z (documented for MSO5000/DS2000); PDUTy reply is a
# fraction on the units I remember, scaled x100 here.
_RIGOL = {"period": "PERiod", "frequency": "FREQuency", "pwidth": "PWIDth", "nwidth": "NWIDth",
          "duty": "PDUTy", "delay": "RDELay"}
# Keysight/Agilent InfiniiVision: `:MEASure:<item>? CHANnel<n>`. DELay? default edges are
# rising/rising, first source to second. Unsure: DUTYcycle replies percent (as remembered).
_KEYSIGHT = {"period": "PERiod", "frequency": "FREQuency", "pwidth": "PWIDth",
             "nwidth": "NWIDth", "duty": "DUTYcycle", "delay": "DELay"}
# Siglent SDS: `C<n>:PAVA? <item>` replies `C1:PAVA PER,1.0E-03S`; delay via
# `C1-C2:MEAD? FRR` (first rising to first rising). Unsure: MEAD syntax across SDS1000X-E /
# SDS2000X; newer firmware may prefer :MEASure:SIMPle:VALue?. Duty reply may carry a '%'.
_SIGLENT = {"period": "PER", "frequency": "FREQ", "pwidth": "PWID", "nwidth": "NWID",
            "duty": "DUTY"}
# Tektronix (legacy immediate measurement, MSO/DPO 2k/3k/4k/5k/70k): set source/type, then
# `:MEASUrement:IMMed:VALue?`. Unsure: newer 5/6 Series MSO still accept IMMed but it is
# deprecated; PDUty replies percent; delay edge/direction setup as remembered.
_TEK = {"period": "PERIod", "frequency": "FREQuency", "pwidth": "PWIdth", "nwidth": "NWIdth",
        "duty": "PDUty", "delay": "DELay"}


def plan(vendor: str, kind: str, ch: int, ch2: int | None = None) -> tuple[list[str], str, float]:
    """-> (setup writes, query, scale to SI unit applied to the parsed reply)."""
    if kind not in KINDS:
        raise MeasureError(f"unknown measurement {kind!r}; choose from {', '.join(KINDS)}")
    if kind == "delay" and ch2 is None:
        ch2 = 2
    if vendor == "rigol":
        extra = f",CHANnel{ch2}" if kind == "delay" else ""
        return [], f":MEASure:ITEM? {_RIGOL[kind]},CHANnel{ch}{extra}", 100.0 if kind == "duty" else 1.0
    if vendor == "keysight":
        extra = f",CHANnel{ch2}" if kind == "delay" else ""
        return [], f":MEASure:{_KEYSIGHT[kind]}? CHANnel{ch}{extra}", 1.0
    if vendor == "siglent":
        if kind == "delay":
            return [], f"C{ch}-C{ch2}:MEAD? FRR", 1.0
        return [], f"C{ch}:PAVA? {_SIGLENT[kind]}", 1.0
    setup = [f":MEASUrement:IMMed:SOURCE1 CH{ch}", f":MEASUrement:IMMed:TYPe {_TEK[kind]}"]
    if kind == "delay":
        setup = [f":MEASUrement:IMMed:SOURCE1 CH{ch}", f":MEASUrement:IMMed:SOURCE2 CH{ch2}",
                 ":MEASUrement:IMMed:DELay:EDGE1 RISe", ":MEASUrement:IMMed:DELay:EDGE2 RISe",
                 ":MEASUrement:IMMed:DELay:DIRection FORWards", f":MEASUrement:IMMed:TYPe {_TEK[kind]}"]
    return setup, ":MEASUrement:IMMed:VALue?", 1.0


_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def parse_reply(reply: str) -> float:
    """Pull the number out of a reply ('1.0E-03', 'C1:PAVA PER,1.0E-03S', '50.0%')."""
    text = reply.strip()
    if text.startswith("C") and ":" in text:  # Siglent echoes the command before the value
        text = text.split(",", 1)[-1] if "," in text else text.split(":", 1)[-1]
    if not text or "*" in text:
        raise MeasureError("no valid measurement: check probe/channel/trigger")
    m = re.search(_NUM, text)
    if not m:
        raise MeasureError("no valid measurement: check probe/channel/trigger "
                           f"(unparseable reply {reply!r})")
    v = float(m.group())
    if not math.isfinite(v) or abs(v) >= INVALID:
        raise MeasureError("no valid measurement: check probe/channel/trigger")
    return v


# --- measuring --------------------------------------------------------------------------

_SCALES = {"s": [(1.0, "s"), (1e-3, "ms"), (1e-6, "µs"), (1e-9, "ns")],
           "Hz": [(1e6, "MHz"), (1e3, "kHz"), (1.0, "Hz")]}


def human(value: float, unit: str, digits: int = 4) -> str:
    """1.0020e-3,'s' -> '1.002 ms'."""
    if unit == "%":
        return f"{value:.{digits}g} %"
    if value == 0:
        return f"0 {unit}"
    for factor, name in _SCALES[unit]:
        if abs(value) >= factor:
            return f"{value / factor:.{digits}g} {name}"
    factor, name = _SCALES[unit][-1]
    return f"{value / factor:.{digits}g} {name}"


def _stats(vals: list[float]) -> dict[str, float]:
    return {"mean": statistics.fmean(vals), "min": min(vals), "max": max(vals),
            "std": statistics.stdev(vals) if len(vals) > 1 else 0.0}


def measure(scope: Any, kind: str, channel: int = 1, channel2: int | None = None,
            samples: int = 20, interval_s: float = 0.2) -> dict[str, Any]:
    """Sample the single-shot measurement `samples` times and compute the statistics."""
    if samples < 1:
        raise MeasureError("--samples must be at least 1")
    idn = scope.idn
    vendor = detect_vendor(idn)
    if kind == "delay" and channel2 is None:
        channel2 = 2
    setup, query, scale = plan(vendor, kind, channel, channel2)
    vals: list[float] = []
    for i in range(samples):
        if i and interval_s > 0:
            time.sleep(interval_s)
        for cmd in setup:
            scope.write(cmd)
        vals.append(parse_reply(scope.query(query)) * scale)
    unit = KIND_UNIT[kind]
    st = _stats(vals)
    out: dict[str, Any] = {"vendor": vendor, "idn": idn, "kind": kind, "channel": channel,
                           "unit": unit, "query": query, "samples": vals, "n": len(vals), **st}
    if kind == "delay":
        out["channel2"] = channel2
    out["human"] = {k: human(st[k], unit) for k in ("mean", "min", "max", "std")}
    return out


def raw_query(scope: Any, cmd: str) -> dict[str, Any]:
    return {"idn": scope.idn, "query": cmd, "reply": scope.query(cmd)}


# --- criteria ---------------------------------------------------------------------------

_UNITS = {"ns": (1e-9, "s"), "us": (1e-6, "s"), "µs": (1e-6, "s"), "μs": (1e-6, "s"),
          "ms": (1e-3, "s"), "s": (1.0, "s"), "hz": (1.0, "Hz"), "khz": (1e3, "Hz"),
          "mhz": (1e6, "Hz"), "%": (1.0, "%")}
_U = r"(ns|us|µs|μs|ms|s|hz|khz|mhz|%)?"
_STAT = re.compile(r"^(max|min|mean|avg)\b\s*", re.IGNORECASE)
_BETWEEN = re.compile(rf"^between\s+({_NUM})\s*{_U}\s+and\s+({_NUM})\s*{_U}$", re.IGNORECASE)
_TOL = re.compile(rf"^({_NUM})\s*{_U}\s*(?:±|\+/-|\+-)\s*({_NUM})\s*{_U}$", re.IGNORECASE)
_CMP = re.compile(rf"^(<=|>=|≤|≥|==|<|>|=)\s*({_NUM})\s*{_U}$", re.IGNORECASE)


def parse_criterion(text: str, base_unit: str | None = None) -> dict[str, Any]:
    """-> {stat, op, lo, hi (SI), unit (SI dimension), disp (unit to display/record in)}."""
    s = " ".join(text.strip().split())
    stat = "mean"
    m = _STAT.match(s)
    if m:
        stat = {"avg": "mean"}.get(m.group(1).lower(), m.group(1).lower())
        s = s[m.end():]

    def conv(num: str, unit: str | None) -> tuple[float, str, str | None]:
        u = (unit or "").lower()
        if not u:
            if base_unit is None:
                raise MeasureError(f"criterion {text!r}: add a unit (ns, us, ms, s, Hz, kHz, MHz, %)")
            return float(num), base_unit, None
        f, dim = _UNITS[u]
        return float(num) * f, dim, unit

    def done(lo, hi, op, dim, disp):
        if base_unit and dim != base_unit:
            raise MeasureError(f"criterion {text!r} is in {dim} but the measurement is in {base_unit}")
        return {"stat": stat, "op": op, "lo": lo, "hi": hi, "unit": dim, "disp": disp or dim}

    if (m := _BETWEEN.match(s)):
        a, da, ua = conv(m.group(1), m.group(2) or m.group(4))
        b, db, ub = conv(m.group(3), m.group(4) or m.group(2))
        if da != db:
            raise MeasureError(f"criterion {text!r} mixes units")
        return done(min(a, b), max(a, b), "between", da, ua or ub)
    if (m := _TOL.match(s)):
        v, dim, disp = conv(m.group(1), m.group(2))
        tol = float(m.group(3))
        tu = (m.group(4) or "").lower()
        if tu == "%" and dim != "%":
            d = abs(v) * tol / 100
        elif tu in ("", "%") and dim == "%":
            d = tol  # percentage points around a percent value
        elif tu:
            f, tdim = _UNITS[tu]
            if tdim != dim:
                raise MeasureError(f"criterion {text!r} mixes units")
            d = tol * f
        else:
            raise MeasureError(f"criterion {text!r}: give the tolerance a unit or %")
        return done(v - d, v + d, "±", dim, disp)
    if (m := _CMP.match(s)):
        op = {"≤": "<=", "≥": ">=", "=": "=="}.get(m.group(1), m.group(1))
        v, dim, disp = conv(m.group(2), m.group(3))
        return done(v if op in (">", ">=", "==") else None, v if op in ("<", "<=", "==") else None,
                    op, dim, disp)
    raise MeasureError(
        f"can't parse criterion {text!r}; forms: '< 1.6 ms', '<= 12 ms', '1 ms ±2%', "
        "'between 0.9 and 1.1 ms', 'max < 2.67 ms'")


def criterion_stat(criterion: str) -> str:
    return parse_criterion(criterion)["stat"]


def check(result: dict, criterion: str) -> tuple[bool, str]:
    """Compare the measured mean (or the stat the criterion names) to the criterion."""
    unit = result["unit"]
    c = parse_criterion(criterion, unit)
    v = result[c["stat"]]
    op, lo, hi = c["op"], c["lo"], c["hi"]
    if op in ("between", "±"):
        ok = lo <= v <= hi
    elif op == "<":
        ok = v < hi
    elif op == "<=":
        ok = v <= hi
    elif op == ">":
        ok = v > lo
    elif op == ">=":
        ok = v >= lo
    else:
        ok = math.isclose(v, lo, rel_tol=1e-6)
    what = f"{c['stat']} {human(v, unit)}"
    return ok, f"{what} {'meets' if ok else 'violates'} {criterion!r}"


def to_unit(value: float, unit: str, disp: str) -> float:
    """SI value -> value in the display unit ('ms'), rounded to 6 significant figures."""
    f = _UNITS.get(disp.lower(), (1.0, unit))[0]
    return float(f"{value / f:.6g}")


def evidence_args(result: dict, criterion: str) -> dict[str, Any]:
    """value/unit/source for evidence.add, from a result and the criterion it was judged by."""
    c = parse_criterion(criterion, result["unit"])
    value = to_unit(result[c["stat"]], result["unit"], c["disp"])
    ch = f"ch{result['channel']}" + (f"->ch{result['channel2']}" if result.get("channel2") else "")
    h = result["human"]
    return {"value": value, "unit": c["disp"],
            "source": (f"scope {result['idn']}: {result['kind']} {ch}, n={result['n']}, "
                       f"min/mean/max {h['min']}/{h['mean']}/{h['max']}")}


def record(cfg: Any, req: str, result: dict, criterion: str, ok: bool, note: str = "") -> dict:
    from cnlp import evidence

    a = evidence_args(result, criterion)
    return evidence.add(cfg, req, "measured_scope", "pass" if ok else "fail", value=a["value"],
                        unit=a["unit"], criterion=criterion, source=a["source"], note=note)
