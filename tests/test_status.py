import json
from pathlib import Path

import pytest

from cnlp import audit, cli, evidence, status

FIX = Path(__file__).parent / "fixtures" / "phases" / "build-procedure.md"


def _audit(unused):
    af = [{"rule": "AUDIT_UNUSED", "severity": "warning", "gpio": g,
           "message": f"GPIO {g} ({role}): planned but not configured in code yet."}
          for g, role in unused]
    return {"ok": True, "target": "T", "snapshot": {"files": 1, "sha256": "ab"},
            "plan_findings": [], "audit_findings": af, "lint_findings": [],
            "summary": {"errors": 0, "warnings": len(af)}, "notes": []}


def _ev(fail=0):
    rows = [{"req": "F10", "text": "No phantom key from combinations", "status": "missing",
             "strength": "none", "covered_by": ["Ghosting"]},
            {"req": "R9", "text": "Unrelated thing", "status": "missing", "strength": "none",
             "covered_by": []}]
    return {"ok": not fail, "target": "T", "snapshot": {}, "rows": rows, "notes": [],
            "summary": {"total": 2, "pass": 0, "fail": fail, "missing": 2, "stale": 0,
                        "accepted": 0}}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    tgt = tmp_path / "tgt"
    (tgt / "docs").mkdir(parents=True)
    (tgt / "docs" / "build-procedure.md").write_text(FIX.read_text())
    (tmp_path / "audit.yaml").write_text(f"target: {tgt}\n")
    monkeypatch.setattr(audit, "audit", lambda c: _audit([(32, "enc_a"), (26, "bclk"), (21, "sda")]))
    monkeypatch.setattr(evidence, "build", lambda c: _ev())
    return tmp_path / "audit.yaml"


def test_status_dict(cfg):
    r = status.status(cfg)
    assert r["ok"] and r["phase"]["current"]["n"] == 2
    assert (r["phase"]["done_count"], r["phase"]["total"], r["phase"]["last"]) == (2, 4, 3)
    by = {u["gpio"]: u for u in r["unused_pins"]}
    assert by[32]["phase"] == {"n": 3, "name": "Encoder", "status": "not_started"}
    assert by[32]["role"] == "enc_a"
    assert by[26]["phase"]["n"] == 1 and by[21]["phase"] is None
    assert [w["rule"] for w in r["status_findings"]] == ["STATUS_PHASE_DONE_PIN_UNUSED"]
    assert r["status_findings"][0]["gpio"] == 26
    assert [x["req"] for x in r["evidence"]["relevant"]] == ["F10"]
    assert r["next_check"].startswith("all keys read")
    assert r["realtime"]["clean"]


def test_not_ok_on_fail(cfg, monkeypatch):
    monkeypatch.setattr(evidence, "build", lambda c: _ev(fail=1))
    assert status.status(cfg)["ok"] is False


def test_missing_procedure(cfg, monkeypatch):
    cfg.write_text(cfg.read_text() + "procedure: docs/none.md\n")
    r = status.status(cfg)
    assert r["phase"]["current"] is None and any("procedure" in n for n in r["notes"])


def test_cli_text_and_json(cfg, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(audit, "find_config", lambda p: cfg)
    rc = cli.main(["status", "x"])
    out = capsys.readouterr().out
    assert rc == 0 and "phase 2 of 3: Matrix (not started)" in out and "[2 of 4 done]" in out
    assert "GPIO 32" in out and "phase 3 Encoder" in out
    assert "Requirements this phase can evidence: F10 (Ghosting)" in out
    assert "STATUS_PHASE_DONE_PIN_UNUSED" not in out and "marked done" in out
    rc = cli.main(["status", "x", "--json"])
    assert json.loads(capsys.readouterr().out)["phase"]["current"]["name"] == "Matrix"
