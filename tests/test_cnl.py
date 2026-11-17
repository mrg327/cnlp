from pathlib import Path

import pytest

from cnlp import cnl
from cnlp.manifest import load

ROOT = Path(__file__).parent.parent
BASE = "chip is esp32.\nmodule is esp32-wroom-32.\nboard is b1.\n"

FULL = BASE + """
# comment
wifi is on.
bluetooth is on.
deep sleep is on.
jtag is on.
console is on nothing.
gpio 34 is input adc "Battery Level" with no pull.
gpio 5 is output pwm "fan" with off-board pull-down.
gpio 13 is input interrupt "wake btn" with internal pull-down wakes from deep sleep on high.
gpio 15 is open-drain "bus_a" with external pull-up.
gpio 25 is output dac "audio" with no pull.
gpio 16 is uart1 rx with internal pull-up.
gpio 17 is uart1 tx "modem_tx" with no pull.
gpio 18 is spi2 sclk with no pull.
gpio 19 is spi2 miso with no pull.
gpio 23 is spi2 mosi with no pull.
gpio 5 is high at reset.
gpio 15 is low at reset.
gpio 4 is input "dup" with no pull.
gpio 4 is output "dup2" with no pull.
"""


def test_blinky_matches_fixture():
    m = cnl.parse((ROOT / "examples/blinky/spec.cnl").read_text())
    assert m == load(ROOT / "tests/fixtures/manifests/blinky.yaml")
    assert all(p.source is None for p in m.pins)


@pytest.mark.parametrize("text", [(ROOT / "examples/blinky/spec.cnl").read_text(), FULL, BASE])
def test_round_trip_and_fixed_point(text):
    m = cnl.parse(text)
    r = cnl.render(m)
    assert cnl.parse(r) == m
    assert cnl.render(cnl.parse(r)) == r


def test_full_details():
    m = cnl.parse(FULL)
    by = {p.role: p for p in m.pins}
    assert m.features.console == "none" and m.features.deep_sleep
    assert by["battery_level"].function == "adc"
    assert by["fan"].pull == "external_offboard"
    assert by["wake_btn"].wake == "high"
    assert by["uart1_rx"].mode == "input" and by["uart1_rx"].bus == "uart1_rx"
    assert by["modem_tx"].mode == "output" and by["modem_tx"].function == "uart"
    assert by["fan"].reset_level == "high"
    assert by["bus_a"].mode == "open_drain" and by["bus_a"].reset_level == "low"
    assert by["spi2_miso"].mode == "input"
    assert len([p for p in m.pins if p.gpio == 4]) == 2
    assert m.idf_version == "5.5.6"


def test_comments_blank_lines_whitespace():
    m = cnl.parse("# hi\n\n  chip   is\n esp32 .\n" + BASE.split("\n", 1)[1] + "   # not a comment-free\n"[:0])
    assert m.chip == "esp32"


def err(text):
    with pytest.raises(cnl.CnlError) as ei:
        cnl.parse(text)
    return ei.value.to_list()


def test_missing_chip():
    e = err("module is m.\nboard is b.\n")
    assert "chip" in e[0]["message"] and e[0]["line"] == 1


def test_unknown_mode():
    e = err(BASE + 'gpio 4 is sideways "x" with no pull.\n')
    assert e[0]["line"] == 4 and "mode" in e[0]["message"] and "'sideways'" in e[0]["message"]


def test_missing_pull():
    e = err(BASE + '\ngpio 4 is input "button".\n')
    assert e[0]["line"] == 5
    assert e[0]["message"].endswith(
        "GPIO 4: say which pull it has, e.g. 'with internal pull-up', "
        "'with external pull-up' or 'with no pull'.")


def test_missing_role():
    e = err(BASE + "gpio 4 is input with no pull.\n")
    assert "GPIO 4" in e[0]["message"] and "quotes" in e[0]["message"]


def test_bad_token_and_uppercase():
    e = err(BASE + 'gpio 4 is input "x" with sticky pull-up.\n')
    assert e[0]["line"] == 4 and "'sticky'" in e[0]["message"]
    e = err("Chip is esp32.\n" + BASE.split("\n", 1)[1])
    assert e[0]["line"] == 1 and "lowercase" in e[0]["message"]


def test_missing_period_and_bad_signal():
    assert "missing its '.'" in err(BASE + "wifi is on")[0]["message"]
    assert "not a signal" in err(BASE + "gpio 4 is i2c0 tx with no pull.\n")[0]["message"]


def test_state_and_reset_errors():
    assert "State machines aren't supported yet" in err(BASE + "state idle: every 5 s read sensor.\n")[0]["message"]
    assert "no 'gpio 7 is" in err(BASE + "gpio 7 is high at reset.\n")[0]["message"]


def test_multiple_errors_reported():
    assert len(err("chip is esp32.\nwifi is maybe.\ngpio x is input.\n")) >= 3


def test_assumptions():
    m = cnl.parse((ROOT / "examples/blinky/spec.cnl").read_text())
    assert cnl.assumptions(m) == []
    a = cnl.assumptions(m, (ROOT / "examples/blinky/spec.cnl").read_text())
    assert {x["field"] for x in a} == {"features.deep_sleep", "features.jtag"}
