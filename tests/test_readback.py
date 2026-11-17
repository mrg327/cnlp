from pathlib import Path

import pytest

from cnlp import manifest, readback

FIX = Path(__file__).parent / "fixtures"
LOG = (FIX / "readback" / "spike_phase0a.log").read_text()
M = manifest.load(FIX / "manifests" / "spike_phase0a.yaml")


def section(name: str) -> str:
    a = LOG.index(f"=== {name} BEGIN ===")
    return LOG[a : LOG.index(f"=== {name} END ===")]


def wrap(body: str) -> str:
    return f"{readback.BEGIN}\n{body}\n{readback.END}\n"


def test_real_log_matches_via_fallback():
    # The log holds three dumps; the last (FULL) is picked, and it covers pins 4 and 25.
    regs = readback.parse(LOG)
    assert readback.diff(M, regs) == []
    assert regs[4]["pullup"] and regs[4]["input_en"] and regs[4]["func_name"] == "GPIO"
    assert regs[25]["output_en"] is None and regs[25]["sig_out"] == 256
    assert regs[4]["sig_in"] == "(simple GPIO input)"


def test_markers_and_last_block_wins():
    d0 = section("DUMP 0").split("=== DUMP 0 BEGIN ===\n")[1]
    bad = d0.replace("Pullup: 1", "Pullup: 0", 1)
    text = wrap(bad) + "noise\n" + wrap(d0)
    assert readback.diff(M, readback.parse(text)) == []
    text = wrap(d0) + wrap(bad)
    assert [d["field"] for d in readback.diff(M, readback.parse(text))] == ["pullup"]


def test_ansi_and_cr_stripped():
    d0 = section("DUMP 0").split("BEGIN ===\n")[1].replace("\n", "\r\n")
    d0 = d0.replace("IO[4] -", "\x1b[0;32mIO[4] -\x1b[0m")
    assert readback.diff(M, readback.parse(wrap(d0))) == []


def test_pullup_flipped():
    text = LOG.replace("IO[4] -\n  Pullup: 1", "IO[4] -\n  Pullup: 0")
    text = text.replace("IO[4] -\r\n  Pullup: 1", "IO[4] -\r\n  Pullup: 0")
    d = readback.diff(M, readback.parse(text))
    assert len(d) == 1
    assert (d[0]["gpio"], d[0]["field"], d[0]["expected"], d[0]["actual"]) == (4, "pullup", True, False)
    assert d[0]["message"] == (
        "GPIO 4 (button): pull-up should be on per the manifest, but the register shows it off."
    )


def _full_with(old: str, new: str) -> str:
    a = LOG.index("=== FULL BEGIN ===")
    assert old in LOG[a:]
    return LOG[:a] + LOG[a:].replace(old, new, 1)


def test_func_sel_changed():
    old = "IO[25] -\n  Pullup: 0, Pulldown: 0, DriveCap: 2\n  InputEn: 0, OutputEn: [periph_sig_ctrl], OpenDrain: 0\n  FuncSel: 2 (GPIO)\n  GPIO Matrix SigOut ID: 256 (simple GPIO output)\n"
    new = "IO[25] -\n  Pullup: 0, Pulldown: 0, DriveCap: 2\n  InputEn: 0, OutputEn: [periph_sig_ctrl], OpenDrain: 0\n  FuncSel: 0 (IOMUX)\n"
    text = _full_with(old, new)
    d = readback.diff(M, readback.parse(text))
    fields = {x["field"] for x in d}
    assert fields == {"func_sel", "sig_out"}  # sig_out line absent under IOMUX
    f = next(x for x in d if x["field"] == "func_sel")
    assert f["gpio"] == 25 and f["expected"] == 2 and f["actual"] == 0
    assert "GPIO 25 (led)" in f["message"]


def test_pin_removed():
    a = LOG.index("=== FULL BEGIN ===")
    full = LOG[a:]
    i = full.index("IO[25] -")
    j = full.index("IO[", i + 1)
    text = LOG[:a] + full[:i] + full[j:]
    d = readback.diff(M, readback.parse(text))
    assert len(d) == 1 and d[0]["gpio"] == 25 and d[0]["field"] == "present"
    assert "missing" in d[0]["message"]


def test_garbage_line_raises():
    text = _full_with("  SleepSelEn: 0\n\nIO[1] -", "  SleepSelEn: 0\n  Banana: 7\n\nIO[1] -")
    with pytest.raises(readback.ReadbackError, match="Banana: 7"):
        readback.parse(text)


def test_no_dump_raises():
    with pytest.raises(readback.ReadbackError):
        readback.parse("I (1) boot: hello\n")
    with pytest.raises(readback.ReadbackError):
        readback.parse(readback.BEGIN + "\nIO[1] -\n")


def test_parse_full_dump():
    regs = readback.parse(section("FULL").split("BEGIN ===\n")[1] + "\n")
    assert 4 in regs and 25 in regs and len(regs) > 20
    assert any(r["reserved"] for r in regs.values())
    assert all(r["raw"] for r in regs.values())


def test_input_pins_34_39_never_expect_pulls():
    p = manifest.Pin(gpio=35, role="adc_in", mode="input", pull="internal_up", function="gpio")
    assert readback.expected(p)["pullup"] is False


def test_checked_fields():
    out = manifest.Pin(gpio=25, role="led", mode="output", pull="none")
    adc = manifest.Pin(gpio=36, role="x", mode="input", pull="none", function="adc")
    assert "sig_out" in readback.checked_fields(out)
    assert readback.checked_fields(adc) == ["pulldown", "pullup"]
