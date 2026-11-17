from pathlib import Path

from cnlp import cli, whatif

SPEC = """chip is esp32.
module is esp32-wroom-32.
board is devkit-v1.
gpio 35 is input "col 2" with external pull-up.
gpio 27 is input "enc sw" with internal pull-up.
"""


def _cfg(tmp_path: Path) -> Path:
    (tmp_path / "spec.cnl").write_text(SPEC)
    (tmp_path / "audit.yaml").write_text("target: /nonexistent\nspec: spec.cnl\n")
    return tmp_path / "audit.yaml"


def test_move_onto_used_input_only_pin_breaks_rules(tmp_path):
    r = whatif.try_change(_cfg(tmp_path), 'gpio 35 is input "enc sw" with internal pull-up.')
    rules = {f["rule"] for f in r["introduced"]}
    assert not r["ok"] and {"OWNER", "C2"} <= rules
    assert any("moves enc_sw from GPIO 27 to GPIO 35" in n for n in r["applied"])


def test_valid_move_is_ok(tmp_path):
    r = whatif.try_change(_cfg(tmp_path), 'gpio 26 is input "enc sw" with internal pull-up.')
    assert r["ok"] and not [f for f in r["introduced"] if f["severity"] == "error"]


def test_bad_statement_is_a_clear_error(tmp_path):
    import pytest
    with pytest.raises(whatif.WhatIfError):
        whatif.try_change(_cfg(tmp_path), "gpio 26 is sideways")


def test_cli_text(tmp_path, capsys, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("cnlp.audit.find_config", lambda p: cfg)
    code = cli.main(["try", 'gpio 35 is input "enc sw" with internal pull-up.', "."])
    out = capsys.readouterr().out
    assert code == 1 and "would break" in out and "OWNER" in out and "C2" in out
