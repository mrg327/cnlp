import json
import shutil
from pathlib import Path

import pytest
import yaml

from cnlp import audit, evidence

FIX = Path(__file__).parent / "fixtures" / "evidence"


@pytest.fixture
def env(tmp_path, monkeypatch):
    target = tmp_path / "target"
    shutil.copytree(FIX / "target", target)
    (target / ".git" / "refs" / "heads").mkdir(parents=True)
    (target / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (target / ".git" / "refs" / "heads" / "main").write_text("c" * 40 + "\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    shutil.copy(FIX / "requirements.yaml", proj)
    c = proj / "audit.yaml"
    c.write_text(yaml.safe_dump({"target": str(target)}))
    state = {"fired": []}

    def fake(_p):
        return {"snapshot": {"sha256": "ab" * 32}, "notes": [], "summary": {"by_rule": {}},
                "plan_findings": [], "audit_findings": [],
                "lint_findings": [{"rule": r} for r in state["fired"]]}

    monkeypatch.setattr(audit, "audit", fake)
    return c, state


@pytest.fixture
def cfg(env):
    return env[0]


def row(res, rid):
    return next(r for r in res["rows"] if r["req"] == rid)


def test_parse_spec():
    md = (FIX / "target/docs/spec.md").read_text()
    reqs, cov = evidence.parse_spec(md)
    assert list(reqs) == ["F1", "F2", "R1", "R2"]
    assert reqs["R1"]["target"] == "Zero underruns"
    assert cov["R1"] == ["Headroom", "Stress"] and cov["F1"] == ["Stress"]


def test_empty_is_missing_and_ok(cfg):
    res = evidence.build(cfg)
    assert res["ok"] and res["summary"]["missing"] == 4
    assert row(res, "R1")["covered_by"] == ["Headroom", "Stress"]
    assert res["snapshot"]["synth_commit"] == "c" * 40


def test_static_support_not_sufficient(cfg):
    r = row(evidence.build(cfg), "R1")
    assert r["strength"] == "static" and r["status"] == "missing"
    assert r["evidence"][0]["source"].startswith("audit snapshot abababab")


def test_static_fail(env):
    cfg, state = env
    state["fired"] = ["RT1_AUDIO_BLOCKING"]
    res = evidence.build(cfg)
    assert not res["ok"] and row(res, "R1")["status"] == "fail"


def test_add_and_strength(cfg):
    evidence.add(cfg, "R1", "measured_scope", "pass", value=1.2, unit="ms", criterion="< 1.6 ms",
                 note="scope")
    evidence.add(cfg, "R1", "reviewed", "pass")
    res = evidence.build(cfg)
    r = row(res, "R1")
    assert r["strength"] == "measured_scope" and r["status"] == "pass"
    lines = (cfg.parent / "evidence.jsonl").read_text().splitlines()
    rec = json.loads(lines[0])
    assert rec["recorded_by"] == "user" and rec["synth_commit"] == "c" * 40
    assert evidence.STRENGTH.index("measured_scope") < evidence.STRENGTH.index("static")


def test_unaccepted_type_stays_missing(cfg):
    evidence.add(cfg, "F2", "tested", "pass")
    r = row(evidence.build(cfg), "F2")
    assert r["strength"] == "tested" and r["status"] == "missing"


def test_fail_overrides_then_newer_pass_clears(cfg):
    evidence.add(cfg, "R2", "tested", "fail")
    assert row(evidence.build(cfg), "R2")["status"] == "fail"
    evidence.add(cfg, "R2", "tested", "pass")
    assert row(evidence.build(cfg), "R2")["status"] == "pass"


def test_stale(cfg):
    evidence.add(cfg, "F1", "tested", "pass", synth_commit="a" * 40)
    res = evidence.build(cfg)
    assert row(res, "F1")["status"] == "stale" and res["summary"]["stale"] == 1


def test_add_validates(cfg):
    for args in (("X9", "tested", "pass"), ("R1", "vibes", "pass"), ("R1", "tested", "maybe")):
        with pytest.raises(evidence.EvidenceError):
            evidence.add(cfg, *args)
    assert not (cfg.parent / "evidence.jsonl").exists()


def test_write_guard(cfg):
    target = Path(yaml.safe_load(cfg.read_text())["target"])
    rc = cfg.parent / "requirements.yaml"
    d = yaml.safe_load(rc.read_text())
    d["evidence"] = str(target / "evidence.jsonl")
    rc.write_text(yaml.safe_dump(d))
    with pytest.raises(audit.AuditError):
        evidence.add(cfg, "R1", "tested", "pass")
    assert not (target / "evidence.jsonl").exists()


def test_accepted_flag(cfg):
    d = yaml.safe_load((cfg.parent / "requirements.yaml").read_text())
    d["accepted_unverified"] = ["F2"]
    (cfg.parent / "requirements.yaml").write_text(yaml.safe_dump(d))
    assert row(evidence.build(cfg), "F2")["status"] == "accepted"


def test_split_value_parses_units():
    from cnlp.evidence import EvidenceError, _split_value
    assert _split_value("1.001ms", None) == (1.001, "ms")
    assert _split_value("42", "Hz") == (42.0, "Hz")
    assert _split_value(0.9, "ms") == (0.9, "ms")
    assert _split_value("n/a", None) == ("n/a", None)
    import pytest
    with pytest.raises(EvidenceError):
        _split_value("1ms", "s")


def test_measurement_needs_criterion(cfg):
    from cnlp.evidence import EvidenceError
    with pytest.raises(EvidenceError, match="criterion"):
        evidence.add(cfg, "R1", "measured_scope", "pass", value=1.2, unit="ms")
