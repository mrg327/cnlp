import re
import shutil
from pathlib import Path

import pytest
import yaml

from cnlp import audit, evidence, verify
from cnlp.cli import build_parser
from cnlp.output import Style

FIX = Path(__file__).parent / "fixtures" / "verify"


def pin_plan(n_rows: int, n_cols: int) -> str:
    lines = ["chip is esp32.", "module is esp32-wroom-32.", "board is devkit-v1.",
             "wifi is off.", "bluetooth is off.", "console is on uart0."]
    rows, cols = [16, 17, 18, 19, 4], [34, 35, 36, 39, 23]
    lines += [f'gpio {rows[i]} is output "row {i + 1}" with no pull.' for i in range(n_rows)]
    lines += [f'gpio {cols[i]} is input "col {i + 1}" with external pull-up.' for i in range(n_cols)]
    lines += ['gpio 14 is output "probe audio" with no pull.',
              'gpio 2 is output "probe scan" with no pull.']
    return "\n".join(lines) + "\n"


def make(tmp_path, monkeypatch, n_rows=2, n_cols=2, requirements=None):
    target = tmp_path / "target"
    shutil.copytree(FIX / "target", target)
    proj = tmp_path / "proj"
    proj.mkdir()
    shutil.copy(requirements or FIX / "requirements.yaml", proj / "requirements.yaml")
    (proj / "spec.cnl").write_text(pin_plan(n_rows, n_cols))
    cfg = proj / "audit.yaml"
    cfg.write_text(yaml.safe_dump({"target": str(target), "spec": "spec.cnl"}))
    monkeypatch.setattr(audit, "audit", lambda _p: {
        "snapshot": {"sha256": "ab" * 32}, "notes": [], "summary": {"by_rule": {}},
        "plan_findings": [], "audit_findings": [], "lint_findings": []})
    return cfg


def get(res, rid):
    return next(r for r in res["requirements"] if r["req"] == rid)


def commands(row):
    return [s["command"] for s in row["steps"] if s["kind"] == "command"]


def test_parse_tests_and_status(tmp_path, monkeypatch):
    res = verify.plan(make(tmp_path, monkeypatch))
    r1 = get(res, "R1")
    assert r1["tests"] == [{"name": "Headroom", "method": "Toggle GPIO 14; measure on the scope.",
                            "criterion": "Well under buffer period"}]
    assert r1["status"] == "missing" and r1["evidence_types"] == ["measured_scope", "tested"]
    assert [r["req"] for r in res["requirements"]] == ["F1", "F10", "R1", "R4", "R5"]


def test_single_requirement_and_unknown(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    assert [r["req"] for r in verify.plan(cfg, "R4")["requirements"]] == ["R4"]
    with pytest.raises(evidence.EvidenceError):
        verify.plan(cfg, "R99")


def test_measure_command_parses_with_cli(tmp_path, monkeypatch):
    res = verify.plan(make(tmp_path, monkeypatch))
    cmd = commands(get(res, "R1"))[0]
    assert cmd == ('cnlp measure pwidth --ch 1 --criterion "max < 1.6 ms" --record . R1 '
                   '--source-note "GPIO 14 (probe audio)"')
    import shlex
    a = build_parser().parse_args(shlex.split(cmd)[1:])
    assert a.kind == "pwidth" and a.criterion == "max < 1.6 ms" and a.record == ["." , "R1"]
    r4 = commands(get(res, "R4"))[0]
    a = build_parser().parse_args(shlex.split(r4)[1:])
    assert a.criterion == "1 ms ±2%" and a.source_note == "GPIO 2 (probe scan)"
    fallback = commands(get(res, "R4"))[1]
    assert fallback.startswith("cnlp evidence add . R4 measured_scope pass --value")


def test_bad_criterion_gives_note_not_command(tmp_path, monkeypatch):
    req = tmp_path / "req.yaml"
    d = yaml.safe_load((FIX / "requirements.yaml").read_text())
    d["requirements"]["R1"]["verify"]["tests"]["Headroom"]["criterion"] = "fast enough"
    req.write_text(yaml.safe_dump(d))
    row = get(verify.plan(make(tmp_path, monkeypatch, requirements=req)), "R1")
    assert not any(c.startswith("cnlp measure") for c in commands(row))
    assert any("does not parse" in s["text"] for s in row["steps"])


def test_missing_criterion_is_reported(tmp_path, monkeypatch):
    req = tmp_path / "req.yaml"
    d = yaml.safe_load((FIX / "requirements.yaml").read_text())
    del d["requirements"]["R4"]["verify"]["tests"]["Scan period"]["criterion"]
    req.write_text(yaml.safe_dump(d))
    row = get(verify.plan(make(tmp_path, monkeypatch, requirements=req)), "R4")
    assert any("criterion missing" in s["text"] for s in row["steps"])
    assert not any(c.startswith("cnlp measure") for c in commands(row))


def test_unhinted_scope_test_noted(tmp_path, monkeypatch):
    row = get(verify.plan(make(tmp_path, monkeypatch)), "R5")
    assert any("no verify hint" in s["text"] for s in row["steps"])
    assert commands(row)[0].startswith("cnlp evidence add . R5 measured_scope pass")


def test_ghosting_2x2(tmp_path, monkeypatch):
    row = get(verify.plan(make(tmp_path, monkeypatch, 2, 2)), "F10")
    g = row["checklist"]
    assert len(g["singles"]) == 4 and len(g["combos"]) == 4
    c = g["combos"][0]
    assert c["id"] == "G1" and c["phantom"] == "R1C1" and c["phantom_label"] == "R1C1 (a)"
    assert c["press"] == ["R1C2", "R2C1", "R2C2"]
    for c in g["combos"]:
        assert c["phantom"] not in c["press"] and len(c["press"]) == 3


def test_ghosting_5x5_counts(tmp_path, monkeypatch):
    req = tmp_path / "req.yaml"
    d = yaml.safe_load((FIX / "requirements.yaml").read_text())
    del d["requirements"]["F10"]["verify"]["layout"]
    req.write_text(yaml.safe_dump(d))
    res = verify.plan(make(tmp_path, monkeypatch, 5, 5, requirements=req))
    row = get(res, "F10")
    assert len(row["checklist"]["singles"]) == 25 and len(row["checklist"]["combos"]) == 400
    assert len({tuple(c["press"]) for c in row["checklist"]["combos"]}) == 400
    assert any("no key layout" in s["text"] for s in row["steps"])
    assert "R1C1 (" not in row["checklist"]["singles"][0]["label"]
    text = verify.format_text(res, Style(color=False))
    assert "first 6 of 400 combos" in text and "G400" in text
    md = verify.to_markdown(res)
    assert len(re.findall(r"- \[ \] G\d+:", md)) == 400 and len(re.findall(r"- \[ \] K\d+ ", md)) == 25


def test_ghosting_needs_matrix_roles(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    (cfg.parent / "spec.cnl").write_text(pin_plan(1, 1))
    row = get(verify.plan(cfg), "F10")
    assert "checklist" not in row and "at least 2 row_N" in row["steps"][0]["text"]


def test_no_spec_test_marked(tmp_path, monkeypatch):
    res = verify.plan(make(tmp_path, monkeypatch))
    assert res["no_spec_test"] == ["F1"]
    f1 = get(res, "F1")
    assert f1["no_spec_test"] and commands(f1) == ['cnlp evidence add . F1 tested pass --note "<what you did and saw>"']
    assert "No verification-plan test in the spec for: F1" in verify.format_text(res, Style(color=False))


def test_unknown_hint_test_warns(tmp_path, monkeypatch):
    req = tmp_path / "req.yaml"
    d = yaml.safe_load((FIX / "requirements.yaml").read_text())
    d["requirements"]["R1"]["verify"]["tests"]["Nope"] = {"measure": "period"}
    req.write_text(yaml.safe_dump(d))
    res = verify.plan(make(tmp_path, monkeypatch, requirements=req))
    assert any("'Nope'" in n for n in res["notes"])


def test_real_hints_parse():
    """Every measure hint in the repo's synth requirements.yaml has a valid kind and criterion."""
    d = yaml.safe_load((Path(__file__).parents[1] / "projects/synth/requirements.yaml").read_text())
    n = 0
    for meta in d["requirements"].values():
        for th in ((meta.get("verify") or {}).get("tests") or {}).values():
            if th.get("measure"):
                n += 1
                assert verify._validate_criterion(th["measure"], th["criterion"]) is None
    assert n == 3
