import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from cnlp import ask as A
from cnlp import llm
from cnlp.output import Style


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str
    title: str
    text: str
    kind: str


CHUNKS = [
    Chunk("cmd:audit", "cnlp", "audit", "cnlp audit CONFIG: read-only audit", "command"),
    Chunk("cmd:evidence", "cnlp", "evidence", "cnlp evidence add CONFIG REQ TYPE pass|fail", "command"),
    Chunk("cmd:flash", "cnlp", "flash", "cnlp flash --port PORT", "command"),
    Chunk("rule:C2", "rules", "C2", "GPIO 34 to 39 are input only and have no internal pull", "rule"),
    Chunk("req:synth:R4", "spec", "R4", "R4 scan period is 1 ms", "requirement"),
]
COMMANDS = [
    {"name": "audit", "help": "audit", "usage": "cnlp audit CONFIG", "read_only": True, "args": []},
    {"name": "status", "help": "status", "usage": "cnlp status CONFIG", "read_only": True, "args": []},
    {"name": "evidence", "help": "evidence", "usage": "cnlp evidence add CONFIG REQ TYPE pass|fail",
     "read_only": True, "args": []},
    {"name": "flash", "help": "flash", "usage": "cnlp flash --port PORT", "read_only": False, "args": []},
]


class FakeIndex:
    def __init__(self, chunks=CHUNKS):
        self.chunks = list(chunks)

    def retrieve(self, q, k=8):
        return self.chunks[:k]

    def get(self, id):
        return next((c for c in self.chunks if c.id == id), None)


def fake_facts(chunks):
    txt = " ".join(c.text + " " + c.id for c in chunks)
    return {"gpios": {34, 35, 36, 37, 38, 39}, "rule_ids": {"C2"}, "req_ids": {"R4"},
            "commands": {c["name"] for c in COMMANDS}, "numbers": set(), "_": txt}


@pytest.fixture(autouse=True)
def kb(monkeypatch):
    ns = SimpleNamespace(commands=lambda: COMMANDS, facts=fake_facts)
    monkeypatch.setattr(A, "_kb", lambda: ns)
    return ns


class Client:
    LlmError = llm.LlmError

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


def reply(**kw):
    d = {"kind": "unknown", "command": None, "why": "", "answer": None, "sources": [],
         "confidence": "high"}
    d.update(kw)
    return d


def go(q, *replies, **kw):
    c = Client(*replies)
    return A.ask(q, index=FakeIndex(), client=c, **kw), c


def test_valid_command():
    r, _ = go("audit my project", reply(kind="command", command="cnlp audit /tmp/x", why="w",
                                        sources=["cmd:audit"]))
    assert r.kind == "command" and r.command == "cnlp audit /tmp/x"
    assert r.read_only and r.sources == ["cmd:audit"]


@pytest.mark.parametrize("cmd", ["cnlp audit x; rm -rf /", "cnlp audit x | cat", "cnlp audit $(id)",
                                 "cnlp audit x > f", "ls -l", "cnlp bogus", "cnlp audit `id`"])
def test_invalid_command_retried_then_unknown(cmd):
    r, c = go("audit it", reply(kind="command", command=cmd), reply(kind="command", command=cmd))
    assert r.kind == "unknown" and len(c.calls) == 2


def test_invalid_command_retry_recovers():
    r, c = go("status", reply(kind="command", command="cnlp nope"),
              reply(kind="command", command="cnlp status /tmp/x", sources=["cmd:audit"]))
    assert r.kind == "command" and r.command == "cnlp status /tmp/x"
    assert "rejected" in c.calls[1][-1]["content"]


def test_json_failure_retry_then_unknown():
    r, c = go("hi", "not json", "{still not")
    assert r.kind == "unknown" and len(c.calls) == 2


def test_run_read_only(monkeypatch):
    seen = []

    def fake_main(argv):
        seen.append(argv)
        print(json.dumps({"ok": True, "phase": "p3"}))
        return 0
    monkeypatch.setattr("cnlp.cli.main", fake_main)
    r, _ = go("status", reply(kind="command", command="cnlp status /tmp/x"), run=True)
    assert seen == [["status", "/tmp/x", "--json"]]
    assert r.ran == {"ok": True, "phase": "p3"} and "phase: p3" in r.ran_summary


@pytest.mark.parametrize("cmd", ["cnlp flash", "cnlp evidence add /x R4 measured_scope pass",
                                 "cnlp audit /x --save"])
def test_never_runs_state_changing(monkeypatch, cmd):
    monkeypatch.setattr("cnlp.cli.main", lambda argv: pytest.fail("ran"))
    r, _ = go("do it", reply(kind="command", command=cmd), run=True)
    assert r.kind == "command" and r.ran is None and not r.read_only


def test_grounded_answer():
    r, _ = go("which pins have no pull", reply(kind="answer", answer="GPIO 34-39 have no pull (C2).",
                                               sources=["rule:C2"]))
    assert r.kind == "answer" and r.grounded and not r.unsupported


def test_invented_gpio_flagged():
    r, _ = go("q", reply(kind="answer", answer="Use GPIO 12 for it.", sources=["rule:C2"]))
    assert r.kind == "answer" and not r.grounded and r.unsupported == ["GPIO 12"]
    assert r.answer.startswith("Warning")


def test_invented_rule_and_req_and_command_downgrade():
    r, _ = go("q", reply(kind="answer", answer="Rule C99 and R12 apply; run `cnlp deploy`.",
                         sources=[]))
    assert r.kind == "unknown" and not r.grounded
    assert {"C99", "R12", "cnlp deploy"} <= set(r.unsupported)


def test_pass_claim_downgraded():
    r, _ = go("q", reply(kind="answer", answer="R4 passed on the scope.", sources=["req:synth:R4"]))
    assert r.kind == "unknown" and "pass claim for R4" in r.unsupported


def test_pass_claim_ok_with_evidence():
    idx = FakeIndex(CHUNKS + [Chunk("ev:R4", "evidence", "R4", "R4 measured_scope pass 1.001 ms", "evidence")])
    r = A.ask("q", index=idx, client=Client(reply(kind="answer", answer="R4 passed (1.001 ms).",
                                                  sources=["ev:R4"])))
    assert r.kind == "answer" and r.grounded


def test_negated_pass_not_claim():
    r, _ = go("q", reply(kind="answer", answer="R4 has not passed yet.", sources=["req:synth:R4"]))
    assert r.grounded


def test_source_filtering():
    r, _ = go("q", reply(kind="answer", answer="No pull on 34-39.", sources=["rule:C2", "made:up"]))
    assert r.sources == ["rule:C2"] and any("made:up" in n for n in r.notes)


@pytest.mark.parametrize("q,cmd", [("please flash the board for me", "cnlp flash"),
                                   ("record evidence that R4 is fine", "cnlp evidence"),
                                   ("approve the gates", "cnlp gate")])
def test_refusals_give_command_and_do_not_call_model(monkeypatch, q, cmd):
    monkeypatch.setattr("cnlp.cli.main", lambda argv: pytest.fail("ran"))
    c = Client()
    r = A.ask(q, index=FakeIndex(), client=c, run=True)
    assert r.refused and r.kind == "command" and r.command.startswith(cmd)
    assert r.ran is None and not c.calls


def test_refuse_write_code():
    c = Client()
    r = A.ask("write my scan.c for me", index=FakeIndex(), client=c)
    assert r.refused and r.kind == "answer" and "will not write" in r.answer and not c.calls


def test_offline_fallback():
    c = Client(llm.LlmError("down"))
    r = A.ask("how do I audit", index=FakeIndex(), client=c)
    assert r.fallback and r.kind == "command" and r.sources == ["cmd:audit"]
    assert r.command.startswith("cnlp audit")


def test_offline_no_match():
    r = A.ask("weather", index=FakeIndex([CHUNKS[3]]), client=Client(llm.LlmError("down")))
    assert r.kind == "unknown" and r.fallback


def test_format_text():
    st = Style(width=80, color=False)
    r, _ = go("q", reply(kind="command", command="cnlp audit /x", why="Audits it.",
                         sources=["cmd:audit"]))
    t = A.format_text(r, st)
    assert "cnlp audit /x" in t and "sources: cmd:audit" in t
    r, _ = go("q", reply(kind="answer", answer="Use GPIO 12.", sources=[]))
    assert "unsupported: GPIO 12" in A.format_text(r, st)
    r, _ = go("q", reply())
    assert "I don't know" in A.format_text(r, st)
    assert A.AskResult().to_dict()["kind"] == "unknown"


@pytest.mark.llm
def test_live_unknown():
    kb = pytest.importorskip("cnlp.knowledge")
    r = A.ask("what's the weather in Paris?", index=kb.build_index(live=False))
    assert r.kind == "unknown"


def test_bracketed_sources_and_help():
    r, _ = go("q", reply(kind="command", command="cnlp audit --help", sources=["[cmd:audit]"]))
    assert r.kind == "command" and r.sources == ["cmd:audit"]


def test_quoted_operators_are_allowed_but_bare_ones_are_not():
    from cnlp.ask import validate_command
    assert validate_command('cnlp measure period --criterion "< 1.6 ms"')[0] is not None
    for bad in ["cnlp status . > x.txt", "cnlp status .; rm -rf /", "cnlp status . | cat",
                "cnlp status $(id)", "cnlp status `id`"]:
        assert validate_command(bad)[0] is None, bad
