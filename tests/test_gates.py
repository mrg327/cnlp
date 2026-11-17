import json
import shutil
import sys
import types
from pathlib import Path

import pytest

from cnlp import gates
from cnlp.manifest import Finding

FIX = Path(__file__).parent / "fixtures" / "manifests" / "blinky.yaml"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / ".cnlp").mkdir()
    shutil.copy(FIX, tmp_path / ".cnlp" / "manifest.yaml")
    state = {"findings": [], "text": "The LED blinks."}
    cnl = types.ModuleType("cnlp.cnl")
    cnl.render = lambda m: state["text"]
    cnl.assumptions = lambda m: [{"text": "LED is active high"}]
    rules = types.ModuleType("cnlp.rules")
    rules.check = lambda m: list(state["findings"])
    monkeypatch.setitem(sys.modules, "cnlp.cnl", cnl)
    monkeypatch.setitem(sys.modules, "cnlp.rules", rules)
    monkeypatch.setattr("cnlp.cnl", cnl, raising=False)
    monkeypatch.setattr("cnlp.rules", rules, raising=False)
    return tmp_path, state


def build(p, app=b"app1"):
    b = p / "build"
    (b / "bootloader").mkdir(parents=True, exist_ok=True)
    (b / "partition_table").mkdir(exist_ok=True)
    (b / "bootloader" / "bootloader.bin").write_bytes(b"boot")
    (b / "partition_table" / "partition-table.bin").write_bytes(b"part")
    (b / "blinky.bin").write_bytes(app)
    (b / "project_description.json").write_text(json.dumps({"app_bin": "blinky.bin"}))


def approve(p, only=None):
    g = gates.next_gate(p, only=only)
    ok, msg = gates.answer(p, g["id"], g["hash"], "approve", "ok")
    assert ok, msg
    return g


WARN = Finding("C1", "warning", "strapping pin", 0)
ERR = Finding("C2", "error", "no pull on input-only pin", 35)


def test_ordering_and_skip(env):
    p, _ = env
    g = gates.next_gate(p)
    assert g["name"] == "intent" and "LED is active high" in g["summary"]
    assert g["id"] == f"g-{g['hash'][:8]}-intent"
    later = gates.next_gate(p, only="flash")
    assert later["name"] == "intent" and later["blocked"]
    assert later["reason"] == "Approve the intent gate first"
    approve(p)
    g = gates.next_gate(p)  # hardware_risk skipped: no warnings
    assert g["name"] == "flash" and g["blocked"] and "Build the project first" in g["reason"]


def test_stale_hash_and_unknown_id(env):
    p, _ = env
    g = gates.next_gate(p)
    ok, msg = gates.answer(p, g["id"], "0" * 64, "approve", "")
    assert not ok and "changed since" in msg
    ok, msg = gates.answer(p, "g-deadbeef-flash", g["hash"], "approve", "")
    assert not ok
    assert not (p / "decisions.jsonl").exists()


def test_manifest_change_invalidates_intent(env):
    p, state = env
    approve(p)
    build(p)
    assert gates.next_gate(p)["name"] == "flash"
    state["text"] = "The LED blinks fast."
    assert gates.next_gate(p)["name"] == "intent"


def test_error_blocks_hardware_risk(env):
    p, state = env
    approve(p)
    state["findings"] = [WARN, ERR]
    g = gates.next_gate(p)
    assert g["name"] == "hardware_risk" and g["blocked"]
    assert "E GPIO 35" in g["summary"]
    ok, msg = gates.answer(p, g["id"], g["hash"], "approve", "")
    assert not ok and "never an error" in msg


def test_warning_gate_lines_and_approval(env):
    p, state = env
    approve(p)
    state["findings"] = [WARN]
    g = gates.next_gate(p)
    assert g["name"] == "hardware_risk" and not g["blocked"]
    assert g["summary"].startswith("W4 GPIO 0: strapping pin")
    approve(p)
    assert gates.next_gate(p)["name"] == "flash"


def test_flash_and_approved_image(env):
    p, _ = env
    approve(p)
    assert gates.image_hash(p) is None
    build(p)
    g = gates.next_gate(p)
    assert g["name"] == "flash" and not g["blocked"]
    ih = gates.image_hash(p)
    assert f"image sha256: {ih}" in g["summary"]
    assert not gates.approved_image(p, ih)
    approve(p)
    assert gates.next_gate(p) is None
    assert gates.approved_image(p, ih)
    build(p, app=b"app2")
    assert not gates.approved_image(p, ih)
    assert not gates.approved_image(p, gates.image_hash(p))
    assert gates.next_gate(p)["name"] == "flash"


def test_log_append_only_supersedes_and_md(env):
    p, state = env
    g1 = approve(p)
    first = (p / "decisions.jsonl").read_text()
    state["text"] = "changed"
    g2 = gates.next_gate(p)
    ok, _ = gates.answer(p, g2["id"], g2["hash"], "change", "tweak")
    assert ok and gates.next_gate(p)["name"] == "intent"  # change does not satisfy
    approve(p)
    text = (p / "decisions.jsonl").read_text()
    assert text.startswith(first)
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[0]["supersedes"] is None
    assert rows[1]["supersedes"] == g1["id"]
    assert rows[2]["supersedes"] == rows[1]["gate_id"]
    assert rows[0]["shown_sha256"] == g1["hash"]
    assert rows[0]["ts"][-6] in "+-"
    md = (p / "decisions.md").read_text()
    assert "| intent | approve |" in md and md.index("change") < md.rindex("approve")
