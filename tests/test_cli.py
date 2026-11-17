import json
import re
import shutil
import sys
import types
from pathlib import Path

import pytest

from cnlp import audit, cli

BLINKY = Path(__file__).parent.parent / "examples" / "blinky" / "spec.cnl"
ANSI = re.compile(r"\x1b\[")


@pytest.fixture(autouse=True)
def _plain(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLUMNS", "100")


def _project(tmp_path, bad=False):
    shutil.copy(BLINKY, tmp_path / "spec.cnl")
    if bad:
        t = (tmp_path / "spec.cnl").read_text()
        t = t.replace('gpio 4 is input "button" with internal pull-up',
                      'gpio 35 is input "sensor" with internal pull-up')
        (tmp_path / "spec.cnl").write_text(t)
    return tmp_path


def run(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_translate_and_check_text(tmp_path, capsys):
    p = _project(tmp_path)
    rc, out, _ = run(capsys, "--project", str(p), "translate")
    assert rc == 0 and "round trip: ok" in out and "{" not in out
    rc, out, _ = run(capsys, "--project", str(p), "check-pins")
    assert rc == 0 and "Pins OK" in out and "no findings" in out and "{" not in out


def test_bad_spec_text_and_json(tmp_path, capsys):
    p = _project(tmp_path, bad=True)
    run(capsys, "--project", str(p), "translate")
    rc, out, _ = run(capsys, "--project", str(p), "check-pins")
    assert rc == 1
    assert "1 error" in out and "C2" in out and "GPIO 35" in out and "{" not in out
    assert not ANSI.search(out)
    for argv in (("--json", "--project", str(p), "check-pins"),
                 ("--project", str(p), "check-pins", "--json")):
        rc, out, _ = run(capsys, *argv)
        data = json.loads(out)
        assert rc == 1 and data["ok"] is False
        assert data["findings"][0]["rule"] == "C2"
        assert out == json.dumps(data, indent=2) + "\n"


def test_parse_error_text(tmp_path, capsys):
    (tmp_path / "spec.cnl").write_text("this is nonsense.\n")
    rc, out, _ = run(capsys, "--project", str(tmp_path), "translate")
    assert rc == 1 and "parse error" in out and "1:" in out
    rc, out, _ = run(capsys, "--project", str(tmp_path), "translate", "--json")
    assert rc == 1 and json.loads(out)["errors"]


def test_gen_lint_sync_gate(tmp_path, capsys):
    p = _project(tmp_path)
    run(capsys, "--project", str(p), "translate")
    rc, out, _ = run(capsys, "--project", str(p), "gen")
    assert rc == 0 and "Generated 5 files" in out and "board_pins.c" in out
    rc, out, _ = run(capsys, "--project", str(p), "lint")
    assert rc == 0 and "Lint OK" in out
    rc, out, _ = run(capsys, "--project", str(p), "sync")
    assert rc == 0 and "no hand edits" in out
    rc, out, _ = run(capsys, "--project", str(p), "gate", "next")
    assert rc == 0 and "gate answer g-" in out and "approve" in out and "{" not in out
    rc, out, _ = run(capsys, "--project", str(p), "gate", "next", "--json")
    gate = json.loads(out)
    assert set(gate) >= {"id", "name", "title", "summary", "hash", "blocked"}
    rc, out, _ = run(capsys, "--project", str(p), "gate", "answer", gate["id"], "bad", "approve")
    assert rc == 1 and out.strip()


def test_sync_edit_text(tmp_path, capsys):
    p = _project(tmp_path)
    run(capsys, "--project", str(p), "translate")
    run(capsys, "--project", str(p), "gen")
    f = p / "components" / "board" / "board_pins.c"
    f.write_text(f.read_text() + "\n/* hand edit */\n")
    rc, out, _ = run(capsys, "--project", str(p), "sync")
    assert rc == 1 and "edited" in out and "board_pins.c" in out


def test_error_goes_to_stderr(tmp_path, capsys):
    rc, out, err = run(capsys, "--project", str(tmp_path / "nope"), "check-pins")
    assert rc == 2 and out == "" and err.startswith(("✗", "x "))
    rc, out, err = run(capsys, "--project", str(tmp_path / "nope"), "check-pins", "--json")
    assert rc == 2 and json.loads(out)["ok"] is False


def test_no_ansi_without_tty_and_with_no_color(tmp_path, capsys, monkeypatch):
    from cnlp import output

    class Tty:
        encoding = "utf-8"

        def isatty(self):
            return True

    assert output.Style(Tty()).color
    monkeypatch.setenv("NO_COLOR", "1")
    assert not output.Style(Tty()).color
    assert "\x1b" not in output.Style(Tty()).c("red", "x")


def test_ascii_fallback():
    from cnlp import output

    class Ascii:
        encoding = "ascii"

        def isatty(self):
            return False

    st = output.Style(Ascii())
    assert (st.ok_sym, st.err_sym) == ("ok", "x")


def test_wrapping_width():
    from cnlp import output

    st = output.Style(width=60, color=False)
    fs = [{"rule": "C2", "severity": "error", "gpio": 1, "message": "word " * 60},
          {"rule": "LINT_DIRECT_GPIO", "severity": "warning",
           "message": "main/a.c:22: " + "long " * 40}]
    lines = output.finding_lines(fs, st)
    assert all(len(ln) <= 60 for x in lines for ln in x.splitlines())
    assert lines[0].lstrip().startswith(st.err_sym)


FAKE_AUDIT = {
    "ok": False, "target": "/somewhere/synth",
    "snapshot": {"source": "/somewhere/synth", "files": 12, "sha256": "0177ac7d" + "a" * 56},
    "plan_findings": [{"rule": "C2", "severity": "error", "message": "GPIO 35: bad pull."}],
    "audit_findings": [
        {"rule": "AUDIT_UNUSED", "severity": "warning", "message": "GPIO 4 not configured."},
        {"rule": "AUDIT_UNUSED", "severity": "warning", "message": "GPIO 5 not configured."},
        {"rule": "AUDIT_PULL", "severity": "error", "message": "main/scan.c:22: pull mismatch."}],
    "lint_findings": [{"rule": "LINT_INCLUDE", "severity": "warning",
                       "message": "main/x.c:3: banned include."}],
    "summary": {"errors": 2, "warnings": 3, "planned_pins": 6, "pins_in_code": 4,
                "not_configured_in_code_yet": 2, "files_scanned": 12, "by_rule": {}},
    "notes": ["lint profile p.yaml not found"],
}


def test_audit_fake(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(audit, "find_config", lambda p: tmp_path / "audit.yaml")
    monkeypatch.setattr(audit, "audit", lambda cfg: FAKE_AUDIT)
    rc, out, _ = run(capsys, "audit", "x")
    assert rc == 1 and "{" not in out
    assert "12 files, sha 0177ac7d" in out
    assert "✗ 2 errors, 1 warning" in out  # AUDIT_UNUSED excluded from the verdict
    assert "Plan" in out and "Code vs plan" in out and "Lint" in out
    assert "2 planned pins not configured yet (expected mid-project)" in out
    assert "lint profile p.yaml not found" in out
    assert "AUDIT_UNUSED" not in out
    for argv in (("--json", "audit", "x"), ("audit", "x", "--json")):
        rc, out, _ = run(capsys, *argv)
        assert rc == 1 and json.loads(out) == FAKE_AUDIT


FAKE_EV = {
    "ok": False, "target": "/somewhere/synth", "snapshot": {},
    "rows": [
        {"req": "R1", "text": "Scope latency under 1.6 ms " * 10, "kind": "timing",
         "strength": "measured_scope", "status": "pass", "evidence": [], "covered_by": []},
        {"req": "R2", "text": "Pins", "kind": "pin", "strength": "none", "status": "missing",
         "evidence": [], "covered_by": []}],
    "summary": {"total": 2, "pass": 1, "fail": 0, "missing": 1, "stale": 0, "accepted": 0},
    "notes": [],
}


def test_evidence_fake(monkeypatch, capsys, tmp_path):
    calls = []
    mod = types.ModuleType("cnlp.evidence")
    mod.build = lambda cfg: FAKE_EV

    def add(cfg, req, type_, result, **kw):
        calls.append((req, type_, result, kw))
        return {"req": req, "type": type_, "result": result, "value": kw["value"],
                "unit": kw["unit"], "criterion": kw["criterion"]}

    mod.add = add
    monkeypatch.setitem(sys.modules, "cnlp.evidence", mod)
    import cnlp
    monkeypatch.setattr(cnlp, "evidence", mod, raising=False)
    monkeypatch.setattr(audit, "find_config", lambda p: tmp_path / "audit.yaml")
    rc, out, _ = run(capsys, "evidence", "x")
    assert rc == 1 and "REQ" in out and "STATUS" in out and "STRENGTH" in out
    assert all(len(ln) <= 100 for ln in out.splitlines())
    assert "R2" in out and "missing" in out
    rc, out, _ = run(capsys, "evidence", "add", "x", "R1", "measured_scope", "pass",
                     "--value", "0.92", "--unit", "ms", "--criterion", "< 1.6 ms")
    assert rc == 0
    assert "Recorded R1 measured_scope pass (0.92 ms, < 1.6 ms)" in out
    assert calls[0][3]["unit"] == "ms"
    rc, out, _ = run(capsys, "evidence", "x", "--json")
    assert json.loads(out) == FAKE_EV


def test_evidence_missing_module(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "cnlp.evidence", None)
    import cnlp
    monkeypatch.delattr(cnlp, "evidence", raising=False)
    rc, _, err = run(capsys, "evidence", "x")
    assert rc == 2 and "not available" in err
