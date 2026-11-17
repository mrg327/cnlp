import shutil
from pathlib import Path

import yaml

from cnlp import audit, params

FIX = Path(__file__).parent / "fixtures" / "params"
PROJ = FIX / "proj"


def run(spec, root=PROJ):
    return params.check(root, ["main"], {"source": "SRC", **spec})


def rules(res):
    return [(f.rule, f.severity) for f in res.findings]


def test_sdkconfig_match_mismatch_missing(tmp_path):
    r = run({"sdkconfig": {"CONFIG_FREERTOS_HZ": 1000, "CONFIG_FREERTOS_UNICORE": False}})
    assert r.findings == []
    r = run({"sdkconfig": {"CONFIG_FREERTOS_HZ": 100}})
    assert rules(r) == [("PARAM_SDKCONFIG", "error")]
    assert "is 1000, expected 100 (SRC)" in r.findings[0].message
    r = run({"sdkconfig": {"CONFIG_NOPE": 3, "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ": 240}})
    assert rules(r) == [("PARAM_SDKCONFIG", "warning")] * 2
    assert "ESP-IDF default applies: 160" in r.findings[1].message
    assert "not set" in r.findings[0].message and "default" not in r.findings[0].message


def test_sdkconfig_defaults_and_absent(tmp_path):
    shutil.copytree(PROJ, tmp_path / "p")
    (tmp_path / "p" / "sdkconfig").unlink()
    (tmp_path / "p" / "sdkconfig.defaults").write_text("CONFIG_FREERTOS_HZ=500\n")
    r = run({"sdkconfig": {"CONFIG_FREERTOS_HZ": 1000}}, tmp_path / "p")
    assert "sdkconfig.defaults: CONFIG_FREERTOS_HZ is 500" in r.findings[0].message
    (tmp_path / "p" / "sdkconfig.defaults").unlink()
    r = run({"sdkconfig": {"CONFIG_FREERTOS_HZ": 1000}}, tmp_path / "p")
    assert rules(r) == [("PARAM_SDKCONFIG", "warning")] and "not checked" in r.findings[0].message


def test_constants_chain_and_missing():
    r = run({"constants": {"AUDIO_RATE": 48000, "AUDIO_FRAMES": 128, "NOT_THERE": 1}})
    assert rules(r) == [("PARAM_MISSING", "warning")]
    r = run({"constants": {"AUDIO_RATE": 44100}})
    assert rules(r) == [("PARAM_CONSTANT", "error")]
    assert r.findings[0].message.startswith("main/audio.c:6: AUDIO_RATE is 48000, expected 44100")


def test_driver_fields():
    spec = {"driver_fields": {
        "i2s_chan_config_t.dma_desc_num": 2, "i2s_chan_config_t.dma_frame_num": 128,
        "i2s_std_config_t.clk_cfg.sample_rate_hz": 48000}}
    assert run(spec).findings == []
    spec["driver_fields"]["i2s_chan_config_t.dma_desc_num"] = 3
    spec["driver_fields"]["i2s_std_config_t.clk_cfg.sample_rate_hz"] = 44100
    spec["driver_fields"]["i2s_std_config_t.slot_cfg.data_bit_width"] = 16
    r = run(spec)
    assert rules(r) == [("PARAM_DRIVER_FIELD", "error"), ("PARAM_DRIVER_FIELD", "error"),
                        ("PARAM_MISSING", "warning")]
    assert r.findings[0].message.startswith("main/audio.c:16: i2s_chan_config_t.dma_desc_num is 2")


def test_tasks():
    spec = {"tasks": {
        "audio_task": {"priority": 20, "core": 1},     # literal via macros
        "scan": {"priority": 10, "core": 0},           # by name; macro expr prio; wrong core
        "ui_task": {"priority": 5, "core": 0},         # xTaskCreate: no affinity != 0
        "display_task": {"priority": 2},               # unresolvable priority
        "gone_task": {"priority": 1, "core": 0}}}
    r = run(spec)
    got = {(f.rule, f.message.split(":")[0] + ":" + f.message.split(":")[1]
            if f.message.startswith("main") else "") for f in r.findings}
    assert got == {("PARAM_TASK", "main/other.c:12"), ("PARAM_TASK", "main/other.c:13"),
                   ("PARAM_UNRESOLVED", "main/other.c:14"), ("PARAM_TASK_MISSING", "")}
    msgs = {f.rule + str(i): f.message for i, f in enumerate(r.findings)}
    assert any("task scan is created with core 1, expected 0 (SRC: priority 10, core 0)" in m
               for m in msgs.values())
    assert any("core no affinity (tskNO_AFFINITY)" in m for m in msgs.values())
    assert not any("audio_task" in m for m in msgs.values())
    assert all(f.severity == "warning" for f in r.findings if f.rule != "PARAM_TASK")


def test_derived_period():
    r = run({"constants": {"AUDIO_FRAMES": 128}, "derived": {"buffer_period_ms": {
        "frames": "AUDIO_FRAMES", "rate": "AUDIO_RATE"}}})
    assert r.derived == {"buffer_period_ms": 2.667}


def _cfg(tmp_path, target, spec):
    shutil.copy(FIX.parent / "audit" / "bad" / "spec.cnl", tmp_path / "spec.cnl")
    (tmp_path / "parameters.yaml").write_text(yaml.safe_dump(spec))
    cfg = tmp_path / "audit.yaml"
    cfg.write_text(yaml.safe_dump({"target": str(target), "sources": ["main"],
                                   "parameters": "parameters.yaml"}))
    return cfg


def test_audit_integration_snapshot_only(tmp_path, monkeypatch):
    seen = []
    real = params.check

    def spy(root, sources, spec):
        seen.append(Path(root))
        assert (Path(root) / "sdkconfig").is_file()
        assert (Path(root) / "main" / "audio.c").is_file()
        return real(root, sources, spec)

    monkeypatch.setattr(params, "check", spy)
    real_parse = params.parse_sdkconfig

    def guarded(p):
        assert PROJ.resolve() not in Path(p).resolve().parents  # never the target in place
        return real_parse(p)

    monkeypatch.setattr(params, "parse_sdkconfig", guarded)
    spec = {"constants": {"AUDIO_RATE": 48000},
            "derived": {"buffer_period_ms": {"frames": "AUDIO_FRAMES", "rate": "AUDIO_RATE"}}}
    r = audit.audit(_cfg(tmp_path, PROJ, spec | {"sdkconfig": {"CONFIG_FREERTOS_HZ": 1000}}))
    assert len(seen) == 1 and PROJ.resolve() not in seen[0].resolve().parents
    assert r["param_findings"] == [] and r["summary"]["derived"] == {"buffer_period_ms": 2.667}
    assert r["snapshot"]["files"] == 3  # two .c files + sdkconfig


def test_audit_param_error_fails(tmp_path):
    r = audit.audit(_cfg(tmp_path, PROJ, {"constants": {"AUDIO_RATE": 1}}))
    assert r["ok"] is False
    assert [f["rule"] for f in r["param_findings"]] == ["PARAM_CONSTANT"]
    assert r["summary"]["by_rule"]["PARAM_CONSTANT"] == 1


def test_audit_without_parameters_unchanged(tmp_path):
    cfg = _cfg(tmp_path, PROJ, {})
    d = yaml.safe_load(cfg.read_text())
    del d["parameters"]
    cfg.write_text(yaml.safe_dump(d))
    r = audit.audit(cfg)
    assert r["param_findings"] == [] and r["params"] is None
    assert r["snapshot"]["files"] == 2  # no sdkconfig copied
