import json
import sys
import types

import pytest

from cnlp import audit, cli, measure
from cnlp.measure import MeasureError

IDNS = {
    "rigol": "RIGOL TECHNOLOGIES,DS1054Z,DS1ZA1,00.04",
    "siglent": "Siglent Technologies,SDS1104X-E,SDS1,1.2",
    "keysight": "KEYSIGHT TECHNOLOGIES,DSOX1204G,CN1,02.00",
    "tektronix": "TEKTRONIX,MSO24,C01,1.0",
}


class FakeScope:
    def __init__(self, vendor, replies):
        self.idn = IDNS[vendor]
        self.replies = list(replies)
        self.log = []

    def query(self, cmd):
        self.log.append(("q", cmd))
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return r

    def write(self, cmd):
        self.log.append(("w", cmd))

    def close(self):
        pass


def run(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


@pytest.mark.parametrize("vendor,reply,query", [
    ("rigol", "1.001000E-03", ":MEASure:ITEM? PERiod,CHANnel1"),
    ("keysight", "+1.00100E-03", ":MEASure:PERiod? CHANnel1"),
    ("siglent", "C1:PAVA PER,1.001000E-03S", "C1:PAVA? PER"),
    ("tektronix", "1.001E-3", ":MEASUrement:IMMed:VALue?"),
])
def test_period_per_vendor(vendor, reply, query):
    sc = FakeScope(vendor, [reply])
    r = measure.measure(sc, "period", samples=3, interval_s=0)
    assert r["vendor"] == vendor and r["n"] == 3 and r["unit"] == "s"
    assert r["mean"] == pytest.approx(1.001e-3) and r["human"]["mean"] == "1.001 ms"
    assert ("q", query) in sc.log
    if vendor == "tektronix":
        assert ("w", ":MEASUrement:IMMed:SOURCE1 CH1") in sc.log
        assert ("w", ":MEASUrement:IMMed:TYPe PERIod") in sc.log


def test_delay_and_duty_queries():
    sc = FakeScope("rigol", ["5E-6"])
    measure.measure(sc, "delay", 1, 2, samples=1)
    assert ("q", ":MEASure:ITEM? RDELay,CHANnel1,CHANnel2") in sc.log
    sc = FakeScope("siglent", ["1E-6"])
    measure.measure(sc, "delay", 1, 3, samples=1)
    assert ("q", "C1-C3:MEAD? FRR") in sc.log
    sc = FakeScope("keysight", ["1E-6"])
    measure.measure(sc, "delay", samples=1)
    assert ("q", ":MEASure:DELay? CHANnel1,CHANnel2") in sc.log
    sc = FakeScope("rigol", ["0.25"])
    assert measure.measure(sc, "duty", samples=1)["mean"] == pytest.approx(25)


@pytest.mark.parametrize("reply", ["9.9E37", "9.91E37", "****", "", "abc"])
def test_invalid_reply(reply):
    with pytest.raises(MeasureError, match="no valid measurement"):
        measure.measure(FakeScope("rigol", [reply]), "period", samples=1)


def test_unknown_vendor():
    sc = FakeScope("rigol", ["1"])
    sc.idn = "ACME,X1"
    with pytest.raises(MeasureError, match="--raw-query"):
        measure.measure(sc, "period")


def test_stats():
    sc = FakeScope("rigol", ["1E-3", "2E-3", "3E-3"])
    r = measure.measure(sc, "period", samples=3, interval_s=0)
    assert (r["min"], r["max"]) == (1e-3, 3e-3)
    assert r["mean"] == pytest.approx(2e-3) and r["std"] == pytest.approx(1e-3)
    assert measure.measure(FakeScope("rigol", ["1E-3"]), "period", samples=1)["std"] == 0


def res(**kw):
    base = {"unit": "s", "mean": 1.002e-3, "min": 0.9e-3, "max": 2.5e-3, "std": 0}
    return {**base, **kw}


@pytest.mark.parametrize("crit,ok", [
    ("< 1.6 ms", True), ("< 1 ms", False), ("<= 12 ms", True), ("> 1 ms", True),
    (">= 2 ms", False), ("1 ms ±2%", True), ("1 ms +/-0.1%", False), ("1 ms ±5 us", True),
    ("between 0.9 and 1.1 ms", True), ("between 1.1 ms and 2 ms", False),
    ("max < 2.67 ms", True), ("max < 2 ms", False), ("min > 1 ms", False),
    ("≤ 1100 us", True), ("< 1.6 ms ", True), ("avg < 2000 µs", True),
])
def test_criteria(crit, ok):
    got, why = measure.check(res(), crit)
    assert got is ok and crit.strip() in why


def test_criteria_freq_percent_and_errors():
    assert measure.check(res(unit="Hz", mean=1000.0), "1 kHz ±1%")[0]
    assert measure.check(res(unit="Hz", mean=1000.0), "< 2 kHz")[0]
    assert measure.check(res(unit="%", mean=50.2), "50 % ±1%")[0]
    assert measure.check(res(unit="%", mean=50.2), "between 49 and 51 %")[0]
    with pytest.raises(MeasureError, match="in Hz"):
        measure.check(res(), "< 2 kHz")
    with pytest.raises(MeasureError, match="can't parse"):
        measure.check(res(), "about a millisecond")


def test_discovery_order(monkeypatch):
    monkeypatch.delenv("SCOPE_RESOURCE", raising=False)
    monkeypatch.setenv("SCOPE_HOST", "10.0.0.9")
    opened = []

    class Res:
        def __init__(self, name):
            self.name = name

        def query(self, c):
            if "5555" not in self.name:
                raise OSError("no")
            return IDNS["rigol"] + "\n"

    class RM:
        def open_resource(self, name, open_timeout=0):
            opened.append(name)
            return Res(name)

    sc = measure.connect(rm=RM())
    assert opened == ["TCPIP0::10.0.0.9::INSTR", "TCPIP0::10.0.0.9::hislip0::INSTR",
                      "TCPIP0::10.0.0.9::5025::SOCKET", "TCPIP0::10.0.0.9::5555::SOCKET"]
    assert sc.idn == IDNS["rigol"] and sc.resource.endswith("5555::SOCKET")
    opened.clear()
    monkeypatch.setenv("SCOPE_RESOURCE", "TCPIP0::1.2.3.4::5555::SOCKET")
    measure.connect(rm=RM())
    assert opened == ["TCPIP0::1.2.3.4::5555::SOCKET"]
    with pytest.raises(MeasureError, match="No instrument answered"):
        measure.connect("TCPIP0::x::INSTR", rm=RM())


def test_missing_pyvisa(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyvisa", None)
    with pytest.raises(MeasureError, match="Install the scope extra"):
        measure.connect()


def test_record_calls_evidence(monkeypatch):
    calls = []
    mod = types.ModuleType("cnlp.evidence")
    mod.add = lambda cfg, req, typ, result, **kw: calls.append((cfg, req, typ, result, kw)) or {
        "ts": "t"}
    monkeypatch.setattr("cnlp.evidence.add", mod.add)
    r = measure.measure(FakeScope("rigol", ["1.0E-3"]), "period", samples=2, interval_s=0)
    measure.record("cfg", "R4", r, "max < 2.67 ms", True, "note")
    cfg, req, typ, result, kw = calls[0]
    assert (cfg, req, typ, result) == ("cfg", "R4", "measured_scope", "pass")
    assert kw["value"] == 1.0 and kw["unit"] == "ms" and kw["criterion"] == "max < 2.67 ms"
    assert kw["source"].startswith(f"scope {IDNS['rigol']}: period ch1, n=2, min/mean/max")
    assert kw["note"] == "note"


def patch_connect(monkeypatch, scope):
    monkeypatch.setattr(measure, "connect", lambda resource=None, rm=None: scope)
    monkeypatch.setattr(measure.time, "sleep", lambda s: None)


def test_cli_text_json_record(monkeypatch, capsys, tmp_path):
    patch_connect(monkeypatch, FakeScope("rigol", ["1.0E-3"]))
    rc, out, _ = run(capsys, "measure", "period", "--samples", "4", "--criterion", "1 ms ±2%")
    assert rc == 0 and out.startswith("Period on CH1: mean 1 ms (min 1 ms, max 1 ms")
    assert 'meets "1 ms ±2%"' in out and "n=4" in out
    rc, out, _ = run(capsys, "measure", "period", "--samples", "2", "--json")
    d = json.loads(out)
    assert rc == 0 and d["n"] == 2 and d["kind"] == "period" and d["unit"] == "s"

    calls = []
    monkeypatch.setattr(audit, "find_config", lambda p: tmp_path / "audit.yaml")
    monkeypatch.setattr("cnlp.evidence.add",
                        lambda cfg, req, typ, result, **kw: calls.append((req, typ, result, kw))
                        or {"ts": "t"})
    rc, out, _ = run(capsys, "measure", "period", "--samples", "3", "--criterion", "< 0.5 ms",
                     "--record", "synth", "R4", "--source-note", "gpio2")
    assert rc == 1 and "fails" in out and "Recorded R4 measured_scope fail" in out
    assert calls[0][:3] == ("R4", "measured_scope", "fail") and calls[0][3]["note"] == "gpio2"


def test_cli_errors(monkeypatch, capsys):
    patch_connect(monkeypatch, FakeScope("rigol", ["9.9E37"]))
    rc, _, err = run(capsys, "measure", "period", "--samples", "1")
    assert rc == 2 and "no valid measurement" in err
    rc, _, err = run(capsys, "measure", "period", "--record", "synth", "R4")
    assert rc == 2 and "requires --criterion" in err
    rc, _, err = run(capsys, "measure", "period", "--criterion", "huh")
    assert rc == 2 and "can't parse" in err


def test_cli_raw_query(monkeypatch, capsys):
    patch_connect(monkeypatch, FakeScope("rigol", ["42"]))
    rc, out, _ = run(capsys, "measure", "--raw-query", ":FOO?")
    assert rc == 0 and ":FOO?" in out and "42" in out
