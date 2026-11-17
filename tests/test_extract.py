from pathlib import Path

from cnlp.extract import extract

FIX = Path(__file__).parent / "fixtures" / "audit"


def _ex():
    return extract(FIX / "idioms", ["main"])


def _kinds(ex, pin):
    return [(u.kind, u.mode, u.pull_up, u.driver, u.signal) for u in ex.pins[pin]]


def test_define_pin_and_input_output_mode():
    ex = _ex()
    (u,) = ex.pins[2]
    assert (u.kind, u.mode, u.file, u.function) == ("gpio_config", "input_output", "main/idioms.c", "setup_led")
    assert u.line == 24 and u.pull_up is False


def test_define_masks_with_bit64_and_or():
    ex = _ex()
    assert {12, 13} <= set(ex.pins)
    assert ex.pins[12][0].mode == "output"


def test_set_direction_and_pullup_with_numeric_define():
    kinds = [u.kind for u in _ex().pins[5]]
    assert kinds == ["set_direction", "pullup"]
    assert _ex().pins[5][1].pull_up is True


def test_const_array_loop_mask():
    ex = _ex()
    for p in (16, 17, 18):
        assert _kinds(ex, p)[0][:2] == ("gpio_config", "output")
    for p in (34, 35, 23):
        assert _kinds(ex, p)[0][:2] == ("gpio_config", "input")


def test_assigned_struct_fields():
    ex = _ex()
    assert ex.pins[34][0].pull_up is False


def test_i2s_std_pins_and_unused_ignored():
    ex = _ex()
    got = {p: (u.driver, u.signal) for p, us in ex.pins.items() for u in us if u.kind == "driver" and u.driver == "i2s"}
    assert got == {26: ("i2s", "bclk"), 25: ("i2s", "ws"), 33: ("i2s", "dout")}


def test_i2c_master_internal_pullup_flag():
    ex = _ex()
    for p, sig in ((21, "sda"), (22, "scl")):
        (u,) = ex.pins[p]
        assert (u.driver, u.signal, u.pull_up) == ("i2c", sig, True)


def test_parameter_mask_is_unresolved_not_guessed():
    ex = _ex()
    assert len(ex.unresolved) == 1
    u = ex.unresolved[0]
    assert u.file == "main/idioms.c" and "pin_bit_mask" in u.what and "mask" in u.text
    assert all(x.function != "setup_param" for us in ex.pins.values() for x in us)


def test_to_dict_roundtrips_to_json():
    import json

    json.dumps(_ex().to_dict())
