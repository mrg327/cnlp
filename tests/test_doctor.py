import json
from pathlib import Path

from cnlp import doctor, output
from cnlp.cli import main

REPO = Path(__file__).resolve().parent.parent


def _probes(tmp_path, **kw):
    home = tmp_path / "home"
    idf = tmp_path / "idf"
    idf.mkdir(exist_ok=True)
    (idf / "version.txt").write_text("v5.5.6\n")
    for sub in ("extensions", "skills"):
        d = home / ".pi" / "agent" / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / "cnlp").symlink_to(REPO / "pi" / sub / "cnlp")
    base = {
        "env": {"CNLP_IDF_PATH": str(idf)}, "home": home,
        "which": lambda n: f"/usr/bin/{n}",
        "run": lambda cmd: "v22.23.2" if "node" in cmd[0] else "1.5.2",
        "http_get": lambda url, t: json.dumps({"data": [{"id": "qwen-exec"}]}),
        "has_module": lambda n: True}
    base.update(kw)
    return doctor.Probes(**base)


def _by(res):
    return {c["name"]: c for c in res["checks"]}


def test_all_good(tmp_path):
    res = doctor.run(str(tmp_path), _probes(tmp_path))
    c = _by(res)
    assert c["ESP-IDF"]["status"] == "ok"
    assert c["model server"]["status"] == "ok"
    assert c["pi extension"]["status"] == "ok" and c["pi skill"]["status"] == "ok"
    assert c["node"]["status"] == "ok"
    assert c["project config"]["status"] == "warn"  # tmp dir has no config
    assert res["ok"]


def test_failures_and_warnings(tmp_path):
    def boom(url, t):
        raise OSError("down")

    p = _probes(tmp_path, which=lambda n: None, http_get=boom, has_module=lambda n: False,
                run=lambda cmd: "v20.1.0")
    p.env["CNLP_IDF_PATH"] = str(tmp_path / "nope")
    c = _by(doctor.run(str(tmp_path), p))
    assert c["model server"]["status"] == "warn"
    assert c["ESP-IDF"]["status"] == "fail"
    assert c["pi"]["status"] == "fail" and c["node"]["status"] == "fail"
    assert c["pyvisa (scope)"]["status"] == "skip"
    assert c["cnlp on PATH"]["status"] == "fail"


def test_model_not_listed_and_timeout(tmp_path):
    seen = []

    def get(url, t):
        seen.append((url, t))
        return json.dumps({"data": [{"id": "other"}]})

    c = _by(doctor.run(str(tmp_path), _probes(tmp_path, http_get=get)))
    assert c["model server"]["status"] == "warn" and "qwen-exec" in c["model server"]["detail"]
    assert seen[0][0].endswith("/models") and seen[0][1] == 3.0


def test_bad_symlink_and_node_boundary(tmp_path):
    p = _probes(tmp_path)
    link = p.home / ".pi" / "agent" / "skills" / "cnlp"
    link.unlink()
    link.symlink_to(tmp_path)
    p.run = lambda cmd: "v22.17.9"
    c = _by(doctor.run(str(tmp_path), p))
    assert c["pi skill"]["status"] == "fail"
    assert c["node"]["status"] == "fail"


def test_idf_version_mismatch(tmp_path):
    p = _probes(tmp_path)
    (Path(p.env["CNLP_IDF_PATH"]) / "version.txt").write_text("v5.4.1\n")
    cfg = tmp_path / "projects" / "synth"
    cfg.mkdir(parents=True)
    tgt = tmp_path / "tgt"
    tgt.mkdir()
    (cfg / "audit.yaml").write_text(f"target: {tgt}\nspec: spec.cnl\n")
    (cfg / "spec.cnl").write_text((REPO / "projects/synth/spec.cnl").read_text())
    import os
    os.environ["CNLP_PROJECTS_DIR"] = str(tmp_path / "projects")
    try:
        c = _by(doctor.run(str(tgt), p))
    finally:
        del os.environ["CNLP_PROJECTS_DIR"]
    assert c["spec"]["status"] == "ok"
    assert c["ESP-IDF"]["status"] == "warn" and "pins v5.5.6" in c["ESP-IDF"]["detail"]


def test_fmt_doctor():
    obj = {"ok": False, "checks": [
        {"name": "a", "status": "ok", "detail": "fine", "fix": ""},
        {"name": "b", "status": "fail", "detail": "bad", "fix": "do x"},
        {"name": "c", "status": "skip", "detail": "n/a", "fix": ""}]}
    text = output.fmt_doctor(obj, output.Style(color=False))
    assert "bad" in text and "To fix" in text and "b: do x" in text and "1 check failed" in text


def test_cli_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run", lambda d: {"ok": True, "checks": []})
    assert main(["doctor", "--json", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    monkeypatch.setattr(doctor, "run", lambda d: {"ok": False, "checks": []})
    assert main(["doctor", str(tmp_path)]) == 1
