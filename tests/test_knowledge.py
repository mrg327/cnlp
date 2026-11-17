import shutil
from pathlib import Path

import pytest
import yaml

from cnlp import audit, knowledge

FIX = Path(__file__).parent / "fixtures" / "knowledge"


@pytest.fixture(scope="module")
def static():
    return knowledge.build_index()


@pytest.fixture
def cfg(tmp_path):
    shutil.copytree(FIX / "target", tmp_path / "target")
    shutil.copytree(FIX / "cfg", tmp_path / "proj")
    c = tmp_path / "proj" / "audit.yaml"
    c.write_text(yaml.safe_dump({"target": str(tmp_path / "target"), "sources": ["main"],
                                 "spec": "spec.cnl"}))
    return c


def top(idx, q, n=1):
    return [c.id for c in idx.retrieve(q, 8)][:n]


def test_static_chunks(static):
    kinds = {c.kind for c in static.chunks}
    assert kinds == {"doc", "command", "rule"}
    for cid in ("cmd:audit", "cmd:evidence", "rule:C2", "rule:W3", "rt:RT1_AUDIO_BLOCKING",
                "rule:LINT_INCLUDE", "rule:OWNER", "doc:design#gate-principles"):
        assert static.get(cid), cid
    assert all(len(c.text) <= 1200 for c in static.chunks)
    assert len({c.id for c in static.chunks}) == len(static.chunks)
    assert "Read-only: yes" in static.get("cmd:audit").text
    assert "check-pins" in static.get("cmd:check-pins").text


def test_retrieval_ranking(static):
    assert top(static, "why can't I use a pull-up on GPIO 35") == ["rule:C2"]
    assert top(static, "how do I check my code against the pin plan") == ["cmd:audit"]
    assert top(static, "how do I record a scope measurement") == ["cmd:evidence"]
    assert top(static, "what does RT1 mean") == ["rt:RT1_AUDIO_BLOCKING"]
    assert top(static, "what is rule C2?") == ["rule:C2"]


def test_named_command_always_included(static):
    ids = [c.id for c in static.retrieve("is the flash step safe? what about gate next", 3)]
    assert "cmd:gate-next" in ids and len(ids) <= 3
    assert static.retrieve("zzzqqq", 5) == []


def test_commands_read_only_flags():
    cmds = {c["name"]: c for c in knowledge.commands()}
    ro = {n for n, c in cmds.items() if c["read_only"]}
    assert ro == {"render", "check-pins", "check-fsm", "lint", "audit", "evidence", "status",
                  "gate next", "sync"}
    for n in ("translate", "gen", "spec", "gate", "gate answer", "gate review", "flash",
              "measure", "evidence add", "readback"):
        assert cmds[n]["read_only"] is False, n
    a = cmds["audit"]
    assert a["usage"].startswith("usage") is False and "cnlp audit" in a["usage"]
    assert any(x["dest"] == "config" and x["help"] for x in a["args"])


def test_project_chunks(cfg):
    idx = knowledge.build_index(cfg)
    p = "proj"
    for cid in (f"pin:{p}:gpio35", f"pin:{p}:gpio4", f"req:{p}:R1", f"status:{p}:audit",
                f"status:{p}:evidence", f"spec:{p}:line3"):
        assert idx.get(cid), cid
    assert 'gpio 35 is input "knob"' in idx.get(f"pin:{p}:gpio35").text
    assert "spec.cnl line 9" in idx.get(f"pin:{p}:gpio35").text
    r1 = idx.get(f"req:{p}:R1").text
    assert "Audio meets its deadline" in r1 and "missing" in r1 and "Headroom" in r1
    assert "snapshot" in idx.get(f"status:{p}:audit").source
    assert top(idx, "is R1 verified yet") == [f"req:{p}:R1"]
    assert "rule:C2" in [c.id for c in idx.retrieve("pull-up on GPIO 35", 3)]


def test_not_live_has_static_project_chunks(cfg):
    idx = knowledge.build_index(cfg, live=False)
    assert idx.get("pin:proj:gpio4") and idx.get("req:proj:R1")
    assert "kind timing" in idx.get("req:proj:R1").text
    assert not any(c.id.startswith("status:") for c in idx.chunks)


def test_live_failure_is_a_note(cfg, monkeypatch):
    def boom(_p):
        raise audit.AuditError("target directory not found")

    monkeypatch.setattr(audit, "audit", boom)
    idx = knowledge.build_index(cfg)
    assert any("live audit unavailable" in n for n in idx.notes)
    assert idx.get("pin:proj:gpio35")


def test_bad_config_is_a_note(tmp_path):
    idx = knowledge.build_index(tmp_path / "missing.yaml")
    assert any("project knowledge unavailable" in n for n in idx.notes)
    assert idx.get("cmd:audit")


def test_facts(static):
    f = knowledge.facts([static.get("rule:C2"), static.get("cmd:audit"),
                         knowledge.Chunk("req:x:R4", "s", "t", "R4 needs 1.6 ms on GPIO 12, 13",
                                         "requirement")])
    assert {34, 35, 39, 12, 13} <= f["gpios"]
    assert {"C2"} <= f["rule_ids"]
    assert f["req_ids"] == {"R4"}
    assert "audit" in f["commands"]
    assert "1.6" in f["numbers"] and "35" in f["numbers"]


def test_deterministic(cfg):
    a = knowledge.build_index(cfg)
    b = knowledge.build_index(cfg)
    assert [c.id for c in a.chunks] == [c.id for c in b.chunks]
    q = "which pins are input-only and why"
    assert [c.id for c in a.retrieve(q)] == [c.id for c in b.retrieve(q)]
