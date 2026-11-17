import shutil
from pathlib import Path

import pytest

from cnlp import cnl, codegen, manifest, sync

FIX = Path(__file__).parent / "fixtures" / "manifests" / "blinky.yaml"


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".cnlp").mkdir()
    shutil.copy(FIX, tmp_path / ".cnlp" / "manifest.yaml")
    m = manifest.load(tmp_path / ".cnlp" / "manifest.yaml")
    (tmp_path / "spec.cnl").write_text(cnl.render(m))
    sync.record(tmp_path, codegen.generate(m, tmp_path / "components" / "board"))
    return tmp_path


def _edit(proj, rel, old, new):
    f = proj / "components" / "board" / rel
    t = f.read_text()
    assert old in t
    f.write_text(t.replace(old, new))


def test_clean(proj):
    r = sync.scan(proj)
    assert r["ok"] and not r["edited"] and not r["proposals"]
    assert sync.proposed_spec(proj) is None


def test_move_pin_and_apply(proj):
    _edit(proj, "include/board_pins.h", "PIN_LED GPIO_NUM_25", "PIN_LED GPIO_NUM_26")
    r = sync.scan(proj)
    assert not r["ok"] and not r["unrecognized"]
    (p,) = r["proposals"]
    assert p["kind"] == "move_pin"
    assert p["before"] == 'gpio 25 is output "led" with no pull.'
    assert p["after"] == 'gpio 26 is output "led" with no pull.'
    out = sync.apply(proj)
    assert out["ok"] and "cnlp translate" in out["message"]
    assert (proj / ".cnlp" / "spec.cnl.bak").exists()
    m = cnl.parse((proj / "spec.cnl").read_text())
    assert {p.role: p.gpio for p in m.pins}["led"] == 26


def test_change_pull(proj):
    _edit(proj, "board_pins.c", ".pull_up_en = GPIO_PULLUP_DISABLE", ".pull_up_en = GPIO_PULLUP_ENABLE")
    r = sync.scan(proj)
    (p,) = r["proposals"]
    assert p["kind"] == "change_pull" and not r["unrecognized"]
    assert "pull-up" in p["after"] or "pull" in p["after"]
    assert sync.proposed_spec(proj) != cnl.render(manifest.load(proj / ".cnlp" / "manifest.yaml"))


def test_unrecognized_blocks_apply(proj):
    _edit(proj, "include/board_pins.h", "must be a valid GPIO", "is fine")
    r = sync.scan(proj)
    assert r["unrecognized"] and not r["proposals"]
    out = sync.apply(proj)
    assert not out["ok"]
    assert not (proj / ".cnlp" / "spec.cnl.bak").exists()


def test_remove_define(proj):
    _edit(proj, "include/board_pins.h", "#define PIN_LED GPIO_NUM_25\n", "")
    assert sync.scan(proj)["proposals"][0]["kind"] == "remove_pin"


def test_missing(proj):
    (proj / "components" / "board" / "board_pins.c").unlink()
    r = sync.scan(proj)
    assert not r["ok"] and r["missing"] == ["components/board/board_pins.c"]


def test_no_record(tmp_path):
    r = sync.scan(tmp_path)
    assert not r["ok"] and "cnlp gen" in r["message"]
