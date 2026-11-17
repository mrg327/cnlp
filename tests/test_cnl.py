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
    assert "needs a machine first" in err(BASE + "state idle: every 5 s do read sensor.\n")[0]["message"]
    assert "no 'gpio 7 is" in err(BASE + "gpio 7 is high at reset.\n")[0]["message"]


def test_multiple_errors_reported():
    assert len(err("chip is esp32.\nwifi is maybe.\ngpio x is input.\n")) >= 3


def test_assumptions():
    m = cnl.parse((ROOT / "examples/blinky/spec.cnl").read_text())
    assert cnl.assumptions(m) == []
    a = cnl.assumptions(m, (ROOT / "examples/blinky/spec.cnl").read_text())
    assert {x["field"] for x in a} == {"features.deep_sleep", "features.jtag"}


FSM = BASE + """
machine sensor starts in idle.
state idle: on button press go to measuring.
state measuring: every 10 s do read sensor; on read ok go to idle;
  after 3 read failed go to error.
state error: on button press go to idle.
state done.
"""


def test_machine_parse_and_round_trip():
    m = cnl.parse(FSM)
    (sm,) = m.state_machines
    assert (sm.name, sm.start) == ("sensor", "idle")
    assert [s.name for s in sm.states] == ["idle", "measuring", "error", "done"]
    every, on, after = sm.states[1].transitions
    assert (every.kind, every.period_ms, every.action) == ("every", 10000, "read_sensor")
    assert (on.event, on.target) == ("read_ok", "idle")
    assert (after.kind, after.count, after.event, after.target) == ("after", 3, "read_failed", "error")
    assert sm.states[3].transitions == ()
    text = cnl.render(m)
    assert "state measuring: every 10 s do read sensor; on read ok go to idle; after 3 read failed go to error." in text
    assert cnl.parse(text) == m
    assert cnl.render(cnl.parse(text)) == text
    from cnlp.manifest import from_dict
    assert from_dict(m.to_dict()) == m


def test_durations():
    m = cnl.parse(BASE + "machine a starts in s.\nstate s: every 90 s do x; every 2 min do y; every 250 ms do z.\n")
    assert [t.period_ms for t in m.state_machines[0].states[0].transitions] == [90000, 120000, 250]
    assert "every 90 s do x; every 2 min do y; every 250 ms do z" in cnl.render(m)


@pytest.mark.parametrize("bad, msg", [
    ("machine a starts in s.\nstate s: on go go to t.\n", "reserved word"),
    ("machine a starts in s.\nstate s: on x go t.\n", "go to STATE"),
    ("machine a starts in s.\nstate s: every 5 q do x.\n", "every 10 s do"),
    ("machine a starts in s.\nstate s: after 0 x go to t.\n", "at least 1"),
    ("machine a starts in s.\nstate s: jump to t.\n", "starts with 'on'"),
    ("machine a starts in S.\n", "lowercase"),
    ("machine a.\n", "machine NAME starts in STATE"),
])
def test_machine_errors(bad, msg):
    assert msg in err(BASE + bad)[0]["message"]


def test_timer_round_trip_and_units():
    m = cnl.parse(BASE + "machine a starts in s.\nstate s: after 20 s go to t; after 3 read failed go to t; "
                  "after 90 s go to t; after 2 min go to t; after 250 ms go to t.\nstate t.\n")
    ts = m.state_machines[0].states[0].transitions
    assert [(t.kind, t.period_ms, t.count, t.target) for t in ts] == [
        ("timeout", 20000, None, "t"), ("after", None, 3, "t"), ("timeout", 90000, None, "t"),
        ("timeout", 120000, None, "t"), ("timeout", 250, None, "t")]
    text = cnl.render(m)
    assert ("state s: after 20 s go to t; after 3 read failed go to t; after 90 s go to t; "
            "after 2 min go to t; after 250 ms go to t.") in text
    assert cnl.parse(text) == m and cnl.render(cnl.parse(text)) == text
    assert cnl.parse(BASE + "machine a starts in s.\nstate s: after 60 s go to s.\n").state_machines[0] \
        .states[0].transitions[0].period_ms == 60000
    assert "after 1 min go to s" in cnl.render(
        cnl.parse(BASE + "machine a starts in s.\nstate s: after 60 s go to s.\n"))


@pytest.mark.parametrize("bad", [
    "after 20 s go idle", "after 20 s", "after 0 s go to s", "after 20 s go to on", "after 20 s do x",
])
def test_timer_errors(bad):
    with pytest.raises(cnl.CnlError):
        cnl.parse(BASE + f"machine a starts in s.\nstate s: {bad}.\n")
