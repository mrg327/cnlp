from pathlib import Path

import pytest

from cnlp import manifest, rules

FIX = Path(__file__).parent / "fixtures" / "manifests"


def mk(pins, **features):
    feat = {"wifi": False, "bluetooth": False, "deep_sleep": False, "console": "uart0", "jtag": False}
    feat.update(features)
    full = []
    for i, p in enumerate(pins):
        p = dict(p)
        p.setdefault("role", f"pin{i}_{p['gpio']}")
        p.setdefault("mode", "output")
        p.setdefault("pull", "none")
        full.append(p)
    return manifest.from_dict({
        "chip": "esp32", "module": "esp32-wroom-32", "board": "t", "idf_version": "5.5.6",
        "features": feat, "pins": full,
    })


def sev(m):
    return {(f.rule, f.severity) for f in rules.check(m)}


BAD = {
    "pullup_35": ([{"gpio": 35, "mode": "input", "pull": "internal_up"}], {}, [("C2", "error")]),
    "output_34": ([{"gpio": 34, "mode": "output"}], {}, [("C2", "error")]),
    "gpio7": ([{"gpio": 7}], {}, [("C3", "error"), ("W1", "error")]),
    "gpio20": ([{"gpio": 20}], {}, [("C1", "error")]),
    "adc2_wifi": ([{"gpio": 4, "mode": "input", "function": "adc"}], {"wifi": True}, [("C10", "error")]),
    "ext_pullup_12": ([{"gpio": 12, "mode": "input", "pull": "external_up"}], {}, [("W3", "error")]),
    "wake_5": ([{"gpio": 5, "mode": "input", "pull": "internal_up", "wake": "low"}], {}, [("C14", "error")]),
    "int_36_wifi_sleep": (
        [{"gpio": 36, "mode": "input", "function": "interrupt"}],
        {"wifi": True, "deep_sleep": True}, [("C11", "error")]),
    "int_36_adc": (
        [{"gpio": 36, "mode": "input", "function": "interrupt"},
         {"gpio": 39, "mode": "input", "function": "adc"}], {}, [("C11", "error")]),
    "gpio1_console": ([{"gpio": 1}], {}, [("C12", "error")]),
    "i2c_no_pullup": (
        [{"gpio": 21, "mode": "open_drain", "function": "i2c", "bus": "i2c0_sda"}], {},
        [("OPEN_DRAIN_PULL", "error")]),
    "floating": ([{"gpio": 4, "mode": "input"}], {}, [("FLOATING", "error")]),
    "two_owners": ([{"gpio": 25}, {"gpio": 25, "role": "other"}], {}, [("OWNER", "error")]),
    "dac_wrong": ([{"gpio": 27, "function": "dac"}], {}, [("C15", "error")]),
}


@pytest.mark.parametrize("name", BAD)
def test_known_bad(name):
    pins, feats, expected = BAD[name]
    got = sev(mk(pins, **feats))
    for e in expected:
        assert e in got, (name, got)


@pytest.mark.parametrize("fixture", ["blinky.yaml", "spike_phase0a.yaml"])
def test_fixtures_have_no_errors(fixture):
    found = rules.check(manifest.load(FIX / fixture))
    assert [f for f in found if f.severity == "error"] == []
    assert found == []


def test_strapping_button_is_warning():
    found = rules.check(mk([{"gpio": 0, "mode": "input", "pull": "internal_up", "function": "interrupt"}]))
    assert {f.rule for f in found} >= {"C5", "W4"}
    assert all(f.severity == "warning" for f in found)


def test_w4_only_when_reset_level_undeclared():
    m = mk([{"gpio": 0, "mode": "input", "pull": "internal_up"}])
    assert any("at reset" in f.message for f in rules.check(m) if f.rule == "W4")
    m = mk([{"gpio": 0, "mode": "input", "pull": "internal_up", "reset_level": "high"}])
    assert not any(f.rule == "W4" for f in rules.check(m))


def test_gpio12_output_warns_unless_low_at_reset():
    assert ("W3", "warning") in sev(mk([{"gpio": 12, "mode": "output", "pull": "none"}]))
    m = mk([{"gpio": 12, "mode": "output", "pull": "none", "reset_level": "low"}])
    assert not any(f.rule == "W3" for f in rules.check(m))


def test_adc2_plain_gpio_with_wifi_is_warning():
    m = mk([{"gpio": 4, "mode": "output", "pull": "none"}], wifi=True)
    assert ("C10", "warning") in sev(m)
    m = mk([{"gpio": 4, "mode": "output", "pull": "none"}])
    assert not any(f.rule == "C10" for f in rules.check(m))


def test_i2c_internal_pullup_is_weak_warning():
    m = mk([
        {"gpio": 21, "role": "i2c0_sda", "mode": "open_drain", "pull": "internal_up",
         "function": "i2c", "bus": "i2c0_sda"},
    ])
    assert ("I2C_WEAK_PULL", "warning") in sev(m)


@pytest.mark.parametrize("gpio", [37, 38])
def test_gpio37_38_not_on_wroom(gpio):
    assert ("W5", "error") in sev(mk([{"gpio": gpio, "mode": "input", "pull": "external_up"}]))


def test_gpio12_input_warning_and_low_ok():
    assert ("W3", "warning") in sev(mk([{"gpio": 12, "mode": "input", "pull": "internal_down"}]))
    m = mk([{"gpio": 12, "mode": "input", "pull": "internal_down", "reset_level": "low"}])
    assert not any(f.rule == "W3" for f in rules.check(m))


def test_psram_pins_allowed_on_wroom():
    assert rules.check(mk([{"gpio": 16}, {"gpio": 17}])) == []


def test_adc_input_not_floating_and_34_adc_ok():
    m = mk([{"gpio": 34, "mode": "input", "function": "adc"}], wifi=True)
    assert rules.check(m) == []


def test_jtag_warning_and_console_off():
    f = rules.check(mk([{"gpio": 13}], jtag=True))
    assert ("C13", "warning") in {(x.rule, x.severity) for x in f}
    assert rules.check(mk([{"gpio": 1}], console="none")) == []


def test_order_and_message():
    found = rules.check(mk([{"gpio": 35, "mode": "input", "pull": "internal_up", "role": "button"}, {"gpio": 7}]))
    assert [f.gpio for f in found] == sorted(f.gpio for f in found)
    c2 = next(f for f in found if f.rule == "C2")
    assert "GPIO 35" in c2.message and "button" in c2.message and "25" in c2.message
