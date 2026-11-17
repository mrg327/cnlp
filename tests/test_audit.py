import shutil
from pathlib import Path

import pytest
import yaml

from cnlp import audit

FIX = Path(__file__).parent / "fixtures" / "audit"


def _run(tmp_path, name="bad"):
    shutil.copy(FIX / name / "spec.cnl", tmp_path / "spec.cnl")
    cfg = tmp_path / "audit.yaml"
    cfg.write_text(yaml.safe_dump({"target": str(FIX / name), "sources": ["main"], "spec": "spec.cnl"}))
    return audit.audit(cfg)


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("audit"))


def _by(result, rule):
    return [f for f in result["audit_findings"] if f["rule"] == rule]


def test_not_ok_and_summary(result):
    assert result["ok"] is False
    assert result["summary"]["errors"] >= 6
    assert result["summary"]["not_configured_in_code_yet"] == 2


def test_unused(result):
    fs = _by(result, "AUDIT_UNUSED")
    assert sorted(f["gpio"] for f in fs) == [4, 26]  # 26: the code put bclk on 17
    assert all("not configured in code yet" in f["message"] for f in fs)


def test_unplanned(result):
    (f,) = _by(result, "AUDIT_UNPLANNED")
    assert f["gpio"] == 15 and f["message"].startswith("main/bad.c:")


def test_mode_contradiction(result):
    assert [f["gpio"] for f in _by(result, "AUDIT_MODE")] == [2, 32]  # probe_init sets the planned output as input


def test_pull_findings(result):
    msgs = {f["gpio"]: f["message"] for f in _by(result, "AUDIT_PULL")}
    assert set(msgs) == {16, 34, 27}
    assert "no internal pull" in msgs[34]
    assert "plan says 'none'" in msgs[16]
    assert "never enables" in msgs[27]


def test_driver_mismatch(result):
    fs = _by(result, "AUDIT_DRIVER")
    assert [f["gpio"] for f in fs] == [17]  # bclk on a matrix row; ws/lrc and dout/din are consistent
    assert "row_2" in fs[0]["message"]


def test_conflict_reports_both_sites(result):
    (f,) = _by(result, "AUDIT_CONFLICT")
    assert f["gpio"] == 2 and f["message"].count("main/bad.c:") == 2 and "probe_scan" in f["message"]


def test_idioms_unresolved_finding(tmp_path):
    shutil.copy(FIX / "bad" / "spec.cnl", tmp_path / "spec.cnl")
    cfg = tmp_path / "audit.yaml"
    cfg.write_text(yaml.safe_dump({"target": str(FIX / "idioms"), "sources": ["main"]}))
    r = audit.audit(cfg)
    assert [f["rule"] for f in r["audit_findings"] if f["rule"] == "AUDIT_UNRESOLVED"]


def test_write_guard_refuses_inside_target(tmp_path):
    with pytest.raises(audit.AuditError):
        audit.guard_write(FIX / "bad" / ".cnlp" / "audit.json", FIX / "bad")
    assert audit.guard_write(tmp_path / "x.json", FIX / "bad") == (tmp_path / "x.json").resolve()


def test_write_report_refuses_config_inside_target(tmp_path):
    tgt = tmp_path / "proj"
    tgt.mkdir()
    cfg = tgt / "audit.yaml"
    with pytest.raises(audit.AuditError):
        audit.write_report({"target": str(tgt)}, cfg)
    assert not (tgt / ".cnlp").exists()


def _cfg(tmp_path, target):
    shutil.copy(FIX / "bad" / "spec.cnl", tmp_path / "spec.cnl")
    cfg = tmp_path / "audit.yaml"
    cfg.write_text(yaml.safe_dump({"target": str(target), "sources": ["main"]}))
    return cfg


def test_only_snapshot_is_analyzed(tmp_path, monkeypatch):
    from cnlp import lint

    real_extract, real_lint = audit.extract, lint.run
    seen: list[Path] = []

    def spy_extract(t, sources):
        seen.append(Path(t))
        return real_extract(t, sources)

    def spy_lint(t, *a, **k):
        seen.append(Path(t))
        return real_lint(t, *a, **k)

    monkeypatch.setattr(audit, "extract", spy_extract)
    monkeypatch.setattr(lint, "run", spy_lint)
    r = audit.audit(_cfg(tmp_path, FIX / "bad"))
    real = (FIX / "bad").resolve()
    assert len(seen) == 2
    assert all(p.resolve() != real and real not in p.resolve().parents for p in seen)
    assert not seen[0].exists()  # temp dir cleaned up
    assert r["snapshot"]["source"] == str(FIX / "bad") and r["snapshot"]["files"] == 1
    assert any(f["message"].startswith("main/bad.c:") for f in r["audit_findings"])


def test_snapshot_hash_tracks_source_changes(tmp_path):
    tgt = tmp_path / "proj"
    shutil.copytree(FIX / "bad", tgt)
    (tgt / "build").mkdir()
    (tgt / "build" / "junk.c").write_text("int x;")
    cfgdir = tmp_path / "cfg"
    cfgdir.mkdir()
    cfg = _cfg(cfgdir, tgt)
    h1 = audit.audit(cfg)["snapshot"]
    assert audit.audit(cfg)["snapshot"] == h1
    (tgt / "main" / "bad.c").write_text((tgt / "main" / "bad.c").read_text() + "\n// edit\n")
    h2 = audit.audit(cfg)["snapshot"]
    assert h2["sha256"] != h1["sha256"] and h2["files"] == h1["files"]
