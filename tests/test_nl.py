import json
import time

import pytest

from cnlp import cnl, nl

LED_BTN = (
    '# from: "LED on 25"\ngpio 25 is output "led" with no pull.\n'
    '# from: "button on 4 to ground"\ngpio 4 is input "button" with {pull}.\nEND'
)


class Fake:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, grammar=None, temperature=0.7, max_tokens=0, seed=None):
        self.calls.append({"messages": messages, "grammar": grammar, "seed": seed})
        return self.replies.pop(0)


def sample(pull="no pull"):
    return LED_BTN.format(pull=pull)


def test_agreement_defaults_sources():
    c = Fake([sample()] * 3)
    r = nl.translate("LED on 25, button on 4", client=c)
    assert r.agreed and r.ok and r.parse_failures == 0
    assert len(c.calls) == 3 and len({x["seed"] for x in c.calls}) == 3
    assert all(x["grammar"] for x in c.calls)
    assert r.sources == {25: "LED on 25", 4: "button on 4 to ground"}
    fields = {a["field"] for a in r.assumptions}
    assert {"chip", "module", "board"} <= fields
    assert "board is devkit-v1." in r.canonical
    assert cnl.parse(r.canonical) == r.manifest
    assert "# from:" in r.spec_text and "END" not in r.spec_text
    assert any(a["field"] == "features.wifi" for a in r.assumptions)


def test_majority_and_disagreement_question():
    c = Fake([sample("internal pull-up"), sample("no pull"), sample("no pull")])
    r = nl.translate("x", client=c)
    assert not r.agreed
    assert "with no pull" in r.spec_text
    (d,) = r.disagreements
    assert d["gpio"] == 4 and d["field"] == "pull"
    assert "Runs disagreed on GPIO 4's pull: internal pull-up vs no pull. Which is it?" in r.questions


def test_tie_goes_to_first():
    c = Fake([sample("internal pull-up"), sample("no pull"), sample("external pull-up")])
    r = nl.translate("x", client=c)
    assert "internal pull-up" in r.spec_text


def test_absent_pin_disagreement():
    only_led = '# from: "LED"\ngpio 25 is output "led" with no pull.\nEND'
    r = nl.translate("x", client=Fake([only_led, sample(), sample()]))
    assert any(d["field"] == "present" and d["gpio"] == 4 for d in r.disagreements)


def test_parse_error_retry():
    bad = 'gpio 4 is input "button".\nEND'
    c = Fake([bad, sample(), sample(), sample()])
    r = nl.translate("x", client=c, n=3)
    assert r.parse_failures == 0 and r.ok
    retry = c.calls[1]["messages"]
    assert retry[-1]["role"] == "user" and "Your output had these errors" in retry[-1]["content"]
    assert "pull" in retry[-1]["content"]
    assert retry[-2]["content"] == bad


def test_unrecoverable_counts_failure():
    bad = "gpio 4 is wat.\nEND"
    c = Fake([bad, bad, sample(), sample(), sample()])
    r = nl.translate("x", client=c, n=3)
    assert r.parse_failures == 1 and r.ok and not r.agreed


def test_all_fail():
    bad = "nonsense.\nEND"
    r = nl.translate("x", client=Fake([bad] * 6), n=3)
    assert not r.ok and r.parse_failures == 3 and r.errors and r.questions


def test_explicit_hw_not_duplicated():
    t = "chip is esp32.\nmodule is esp32-wroom-32.\nboard is mine.\n" + sample()
    r = nl.translate("x", client=Fake([t] * 3))
    assert "board is mine." in r.canonical
    assert not any(a.get("source") == "default" for a in r.assumptions)


def test_question_lines_and_rule_questions():
    t = ('# question: Is there a resistor on the board?\n'
         '# from: "button on 35"\ngpio 35 is input "button" with internal pull-up.\nEND')
    r = nl.translate("x", client=Fake([t] * 3))
    assert "Is there a resistor on the board?" in r.questions
    assert len(r.questions) >= 2  # rule checker rejects the pull on GPIO 35
    assert any("35" in q for q in r.questions[1:])


def test_state_machine_sample():
    t = ('gpio 4 is input "button" with internal pull-up.\nmachine m starts in idle.\n'
         "state idle: on button press go to busy.\nstate busy: on button press go to idle.\nEND")
    r = nl.translate("x", client=Fake([t] * 3))
    assert r.ok and r.agreed and r.manifest.state_machines[0].name == "m"


def test_write_project(tmp_path):
    r = nl.translate("LED on 25, button on 4", client=Fake([sample()] * 3))
    nl.write_project(tmp_path / "p", "LED on 25, button on 4", r)
    p = tmp_path / "p"
    assert (p / "request.md").read_text() == "LED on 25, button on 4\n"
    assert "# from:" in (p / "spec.cnl").read_text()
    d = json.loads((p / ".cnlp" / "translation.json").read_text())
    assert d["agreed"] is True and d["sources"]["25"] == "LED on 25"
    text = (p / "spec.cnl").read_text()
    assert "# assumed defaults" in text
    assert cnl.parse(text)  # the written spec parses as is


@pytest.mark.llm
def test_live_translate():
    t = time.time()
    r = nl.translate("LED on 25, button on 4 to ground with nothing else on the board")
    print(f"\n{time.time() - t:.1f}s agreed={r.agreed}\n{r.spec_text}\n{r.questions}")
    assert r.ok
    cnl.parse(r.canonical)


def test_merge_state_lines_joins_split_states():
    text = ("machine s starts in idle.\nstate idle: on start go to busy.\n"
            "state busy: every 10 s do read.\nstate busy: after 3 failed go to idle.\n")
    merged = nl._merge_state_lines(text)
    assert "state busy: every 10 s do read; after 3 failed go to idle." in merged
    assert merged.count("state busy") == 1
