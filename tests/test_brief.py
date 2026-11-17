"""--for-model output: deterministic REPORT / FACTS / NEXT blocks."""

import json
import re

import pytest

from cnlp import audit, brief, cli, status

ID = re.compile(r"^- [✓✗!○] ([A-Z][A-Z0-9_]*)\b")


def f(rule, msg, sev="error", **kw):
    return {"rule": rule, "severity": sev, "message": msg, **kw}


PARAM_TASK = f("PARAM_TASK", "main/audio.c:121: task audio_task is created with core 0, expected 1 "
               "(CLAUDE.md Key parameters: priority 20, core 1).")
CONST = f("PARAM_CONSTANT", "main/audio.h:9: AUDIO_BUF_FRAMES is 64, expected 128 (parameters.yaml).")
FIELD = f("PARAM_DRIVER_FIELD", "main/i2s.c:30: i2s_std_config_t.clk_cfg.sample_rate_hz is 44100, "
          "expected 48000 (parameters.yaml).")
SDK = f("PARAM_SDKCONFIG", "sdkconfig: CONFIG_FREERTOS_HZ is 100, expected 1000 (parameters.yaml).")
RT = {
    "RT1_AUDIO_BLOCKING": f("RT1_AUDIO_BLOCKING", "main/audio.c:50: audio_task calls vTaskDelay"),
    "RT2_QUEUE_TIMEOUT": f("RT2_QUEUE_TIMEOUT", "main/a.c:5: xQueueReceive timeout is portMAX_DELAY"),
    "RT3_SHARED_WRITER": f("RT3_SHARED_WRITER", "main/a.c:6: g_vol written by two tasks", "warning"),
    "RT4_TASK_NO_BLOCK": f("RT4_TASK_NO_BLOCK", "main/a.c:7: loop never blocks"),
    "RT5_DOUBLE": f("RT5_DOUBLE", "main/a.c:8: double literal"),
    "RT6_STARTUP_ORDER": f("RT6_STARTUP_ORDER", "main/a.c:9: audio task not last"),
    "RT7_PERIOD_DRIFT": f("RT7_PERIOD_DRIFT", "main/scan.c:40: scan_task paces its 1 ms loop with vTaskDelay"),
    "RT7_PERIOD_MISMATCH": f("RT7_PERIOD_MISMATCH", "main/scan.c:41: period differs", "warning"),
}
AUDIT_RULES = [
    f("AUDIT_PULL", "main/scan.c:22: GPIO 4 (col_1) enables the internal pull-down but the plan says 'internal_up'."),
    f("AUDIT_MODE", "main/scan.c:23: GPIO 5 (row_1) is planned as an output but the code sets mode GPIO_MODE_INPUT."),
    f("AUDIT_DRIVER", "main/i2s.c:3: i2s dout is mapped to GPIO 5, but it is not allowed."),
    f("AUDIT_CONFLICT", "main/a.c:1: GPIO 6 (x) is configured here and at main/b.c:2."),
    f("AUDIT_UNPLANNED", "GPIO 7 is used in code but not planned.", "warning"),
]
PINS = [
    f("C2", "GPIO 35 (enc_sw): no pull hardware. Free pins that pass every rule for enc_sw: GPIO 27.", gpio=35),
    f("OWNER", "GPIO 35: assigned more than once. Free pins that pass every rule for enc_sw: GPIO 27.", gpio=35),
    f("C6", "GPIO 2 (x): strapping pin.", "warning", gpio=2),
    f("W4", "GPIO 2 (x): level at reset isn't declared. Say 'gpio 2 is low at reset' or 'high at reset' so.",
      "warning", gpio=2),
]
NOT_YET = [
    f("AUDIT_UNUSED", "GPIO 32 (enc_a): planned but not configured.", "warning", gpio=32),
    f("AUDIT_UNUSED", "GPIO 13 (enc_b): planned but not configured.", "warning", gpio=13),
    f("PARAM_TASK_MISSING", "Task scan_task is not created in the code yet (expected core 0).", "warning"),
    f("PARAM_MISSING", "FOO is not defined in the code yet (expected 1).", "warning"),
]


def audit_result(plan=(), code=(), lint=(), param=()):
    return {"ok": True, "target": "/x", "snapshot": {}, "plan_findings": list(plan),
            "audit_findings": list(code), "lint_findings": list(lint),
            "param_findings": list(param), "notes": [],
            "summary": {"planned_pins": 20, "pins_in_code": 15, "files_scanned": 7,
                        "derived": {"buffer_period_ms": 2.667}}}


def finding_lines(text):
    return [ln for ln in text.splitlines() if ID.match(ln)]


def test_param_task_keeps_rule_id_and_plain_core_wording():
    out = brief.brief_audit(audit_result(param=[PARAM_TASK, *NOT_YET]))
    assert "- ✗ PARAM_TASK main/audio.c:121 — audio_task is on core 0; the plan says core 1 " \
           "(CLAUDE.md Key parameters)." in out
    assert out.startswith("REPORT (relay this; keep the rule IDs):")
    assert "FACTS (authoritative; don't re-derive from files):" in out
    assert "NEXT (suggest, don't do):" in out
    assert "- Fix: create audio_task with core 1 at main/audio.c:121" in out
    assert "Re-run cnlp_audit" in out


def test_every_finding_line_starts_with_rule_id_and_not_yet_is_one_line():
    fs = audit_result(plan=PINS, code=[*AUDIT_RULES, *NOT_YET[:2]], lint=list(RT.values()),
                      param=[PARAM_TASK, CONST, FIELD, SDK, NOT_YET[2], NOT_YET[3]])
    out = brief.brief_audit(fs)
    report = out.split("FACTS")[0].splitlines()[1:]
    for ln in report:
        assert ln.startswith("- "), ln
        assert ID.match(ln) or "more (" in ln or "Real-time" in ln, ln
    assert sum("AUDIT_UNUSED" in ln for ln in out.splitlines() if ln.startswith("- ○")) == 1
    assert len([ln for ln in out.splitlines() if ln.startswith("- ○")]) == 1
    assert "(GPIO 32, 13)" in out and "scan_task" in out


def test_line_budget_and_truncation():
    many = [f("AUDIT_PULL", f"main/a.c:{i}: GPIO {i} (x) bad pull") for i in range(1, 60)]
    out = brief.brief_audit(audit_result(code=[*many, *NOT_YET]))
    assert len(out.splitlines()) <= brief.MAX_LINES
    assert re.search(r"- \+\d+ more \(run cnlp audit for all\)", out)
    assert "AUDIT_UNUSED" in out  # summaries survive truncation
    big = brief.brief_audit(audit_result(plan=PINS, code=AUDIT_RULES, lint=list(RT.values()),
                                         param=[PARAM_TASK, CONST, FIELD, SDK]))
    assert len(big.splitlines()) <= brief.MAX_LINES


@pytest.mark.parametrize("finding, expect", [
    (PARAM_TASK, "create audio_task with core 1"),
    (CONST, "change AUDIO_BUF_FRAMES to 128"),
    (FIELD, "set driver field i2s_std_config_t.clk_cfg.sample_rate_hz to 48000"),
    (SDK, "set CONFIG_FREERTOS_HZ"),
    (RT["RT1_AUDIO_BLOCKING"], "lower-priority task"),
    (RT["RT2_QUEUE_TIMEOUT"], "timeout 0"),
    (RT["RT3_SHARED_WRITER"], "single writer"),
    (RT["RT4_TASK_NO_BLOCK"], "blocking call"),
    (RT["RT5_DOUBLE"], "float"),
    (RT["RT6_STARTUP_ORDER"], "audio task is created last"),
    (RT["RT7_PERIOD_DRIFT"], "vTaskDelayUntil"),
    (RT["RT7_PERIOD_MISMATCH"], "period_ms"),
    (AUDIT_RULES[0], "pull setting"),
    (AUDIT_RULES[1], "mode"),
    (AUDIT_RULES[2], "driver signal"),
    (AUDIT_RULES[3], "one owner"),
    (AUDIT_RULES[4], "pin plan"),
    (PINS[0], "cnlp_try"),
    (PINS[1], "Suggested free pins for enc_sw: GPIO 27"),
    (PINS[2], "hardware-risk approval"),
    (PINS[3], "gpio 2 is low at reset"),
])
def test_next_table_covers_each_rule_family(finding, expect):
    steps = brief.next_steps([finding])
    assert any(expect in s for s in steps), steps


def test_next_ignores_not_yet_and_dedups():
    assert brief.next_steps(NOT_YET) == []
    steps = brief.next_steps([PINS[0], PINS[1]])
    assert len([s for s in steps if "cnlp_try" in s]) == 1


FAKE_STATUS = {
    "ok": True, "target": "/x", "snapshot": {},
    "phase": {"current": {"n": 4, "name": "Button matrix", "status": "not_started"},
              "done_count": 4, "total": 12, "last": 11},
    "audit": {"errors": 1, "warnings": 1, "not_yet": 8, "strapping": [2]},
    "realtime": {"clean": True, "findings": []},
    "unused_pins": [{"gpio": 32, "role": "enc_a", "phase": {"n": 7, "name": "Encoder", "status": "not_started"}}],
    "status_findings": [],
    "evidence": {"summary": {"total": 17, "pass": 0, "fail": 0, "missing": 17, "stale": 0, "accepted": 0},
                 "relevant": [{"req": "R4", "tests": ["Scan period"]}], "expected_phase": 10},
    "next_check": "all keys read.", "watch": [f"problem {i}" for i in range(6)], "notes": [],
}


def test_status_brief_counts_only_and_with_findings():
    out = brief.brief_status(FAKE_STATUS)
    assert "- Phase 4 of 11: Button matrix (not started); 4 of 12 phases done." in out
    assert "- Check: all keys read." in out and "- Watch for: problem 0" in out
    assert "Run cnlp_audit for the rule IDs" in out
    assert "- ✓ Real-time rules RT1–RT7: clean." in out
    assert "missing is expected before phase 10" in out
    assert len(out.splitlines()) <= brief.MAX_LINES
    with_f = {**FAKE_STATUS, "_audit_findings": [PARAM_TASK, *NOT_YET]}
    out = brief.brief_status(with_f)
    assert "- ✗ PARAM_TASK main/audio.c:121" in out and "Fix: create audio_task" in out
    assert len(finding_lines(out.split("FACTS")[0])) >= 2
    rt = {**FAKE_STATUS, "realtime": {"clean": False, "findings": [RT["RT7_PERIOD_DRIFT"]]}}
    assert "- ✗ RT7_PERIOD_DRIFT main/scan.c:40" in brief.brief_status(rt)


def test_evidence_brief():
    ev = {"summary": {"total": 3, "pass": 1, "fail": 1, "missing": 1, "stale": 0, "accepted": 0},
          "rows": [{"req": "R1", "status": "pass", "strength": "measured_scope", "text": "Latency"},
                   {"req": "R2", "status": "fail", "strength": "measured_scope", "text": "Period"},
                   {"req": "F1", "status": "missing", "strength": "none", "text": "Note"}],
          "notes": []}
    out = brief.brief_evidence(ev)
    assert "- ✗ R2 fail (measured_scope)" in out and "- ✓ R1 pass" in out
    assert "Missing: F1" in out and "recorded by the user" in out
    assert len(out.splitlines()) <= brief.MAX_LINES


def test_try_brief():
    res = {"ok": False, "change": 'gpio 35 is input "enc sw".', "applied": ["moves enc_sw from GPIO 27 to GPIO 35"],
           "introduced": PINS[:2], "resolved": [f("C9", "GPIO 1: gone", "warning")]}
    out = brief.brief_try(res)
    assert "would break 2 rule(s)" in out
    assert "- ✗ C2 — " in out and "- ✗ OWNER — " in out and "- ✓ C9" in out
    assert "Suggested free pins for enc_sw: GPIO 27." in out
    assert "Nothing was changed" in out
    ok = brief.brief_try({"ok": True, "change": "x", "applied": [], "introduced": [], "resolved": []})
    assert "No new findings" in ok


def test_ask_brief():
    out = brief.brief_ask({"kind": "command", "command": "cnlp audit .", "why": "checks code",
                           "read_only": True, "sources": ["cli"]})
    assert out.startswith("ANSWER") and "- Command: cnlp audit ." in out
    assert "doesn't know" in brief.brief_ask({"kind": "unknown"})
    assert "PARAM_TASK" in brief.brief_ask({"kind": "answer", "answer": "PARAM_TASK checks cores."})


# ---- CLI wiring ---------------------------------------------------------------------------------

@pytest.fixture
def fake_audit(monkeypatch, tmp_path):
    res = audit_result(plan=PINS[2:], param=[PARAM_TASK, *NOT_YET])
    monkeypatch.setattr(audit, "find_config", lambda p: tmp_path / "audit.yaml")
    monkeypatch.setattr(audit, "audit", lambda cfg: res)
    return res


def run(capsys, *argv):
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def test_for_model_flag_either_side_and_json_default_unchanged(fake_audit, capsys):
    rc1, a = run(capsys, "--for-model", "audit", ".")
    rc2, b = run(capsys, "audit", ".", "--for-model")
    assert rc1 == rc2 == 0 and a == b and a.startswith("REPORT")
    _, j = run(capsys, "--for-model", "--json", "audit", ".")
    assert json.loads(j) == fake_audit
    _, plain = run(capsys, "audit", ".")
    assert "REPORT" not in plain and "Code vs plan" in plain


def test_for_model_status_attaches_audit_findings(monkeypatch, fake_audit, capsys, tmp_path):
    monkeypatch.setattr(status, "status", lambda cfg: json.loads(json.dumps(FAKE_STATUS)))
    _, out = run(capsys, "status", ".", "--for-model")
    assert "- ✗ PARAM_TASK main/audio.c:121" in out and "- ! C6" in out
    _, js = run(capsys, "--json", "--for-model", "status", ".")
    assert "_audit_findings" not in json.loads(js)


def test_verify_plan_brief():
    plan = {"requirements": [
        {"req": "R4", "text": "Scan loop runs at 1 ms", "status": "missing", "steps": [
            {"kind": "note", "text": "probe GPIO 2"},
            {"kind": "command", "text": "measure and record", "command": "cnlp measure . R4 --record"}]},
        {"req": "R5", "text": "Keys debounce", "status": "pass", "steps": []}],
        "notes": [], "no_spec_test": []}
    out = brief.BRIEFS["verify_plan"](plan)
    assert out.startswith("REPORT") and "- ○ R4 missing" in out and "- ✓ R5 pass" in out
    assert "the user runs: cnlp measure . R4 --record" in out
    assert "never run them yourself" in out
