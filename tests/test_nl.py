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

    def complete(self, messages, grammar=None, temperature=0.7, max_tokens=0, seed=None,
                 json_schema=None):
        self.calls.append({"messages": messages, "grammar": grammar, "seed": seed,
                           "json_schema": json_schema})
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


# ---- deterministic normalizations --------------------------------------------

def _one(text, request):
    c = Fake([text + "\nEND"] * 3)
    return nl.translate(request, client=c)


def test_output_pull_dropped_when_request_has_no_pull_wording():
    t = '# from: "LED on 25 with a resistor in series"\ngpio 25 is output "led" with external pull-up.'
    r = _one(t, "LED on 25 with a resistor in series, wifi off")
    assert r.manifest.pins[0].pull == "none"
    assert any(a.get("source") == "normalized" and "25" in a["field"] for a in r.assumptions)


def test_output_pull_kept_when_request_asks_for_it():
    t = '# from: "fan on 5 pulled down"\ngpio 5 is output "fan" with external pull-down.'
    r = _one(t, "fan on 5 pulled down")
    assert r.manifest.pins[0].pull == "external_down"


def test_breakout_board_pullup_is_offboard():
    t = ('# from: "sensor on 21"\ngpio 21 is i2c0 sda with external pull-up.\n'
         'gpio 22 is i2c0 scl with external pull-up.')
    r = _one(t, "I2C on 21 and 22. The breakout board has 4.7k pull-ups.")
    assert {p.pull for p in r.manifest.pins} == {"external_offboard"}


def test_board_pullup_stays_external_without_breakout_wording():
    t = '# from: "button on 14"\ngpio 14 is input "button" with external pull-up.'
    r = _one(t, "button on 14 with a 10k pull-up on the board")
    assert r.manifest.pins[0].pull == "external_up"
    assert not [a for a in r.assumptions if a.get("source") == "normalized"]


def test_interrupt_wording_sets_function():
    t = '# from: "button on 4"\ngpio 4 is input "button" with internal pull-up.'
    r = _one(t, "button on IO4 with internal pullup that triggers an interrupt, led on 25")
    assert r.manifest.pins[0].function == "interrupt"


def test_wake_turns_deep_sleep_on():
    t = '# from: "b"\ngpio 14 is input "b" with external pull-up wakes from deep sleep on low.'
    r = _one(t, "b on 14")
    assert r.manifest.features.deep_sleep is True
    assert any(a["field"] == "features.deep_sleep" for a in r.assumptions)


def test_nudge_when_questions_skip_named_pins():
    bad = "# question: what pull?\nwifi is off.\nEND"
    good = '# from: "pot on gpio 34"\ngpio 34 is input adc "pot" with no pull.\nEND'
    c = Fake([bad, good] * 3)
    r = nl.translate("pot on gpio 34, wifi off", client=c)
    assert len(c.calls) == 6
    assert "no statement for it" in c.calls[1]["messages"][-1]["content"]
    assert r.manifest.pins[0].gpio == 34


def test_no_nudge_without_questions():
    c = Fake(["wifi is off.\nEND"] * 3)
    nl.translate("pot on gpio 34", client=c)
    assert len(c.calls) == 3


def test_repeated_pin_statement_dropped():
    t = ('# from: "b"\ngpio 14 is input "b" with external pull-up.\n'
         '# from: "b"\ngpio 14 is input "b" with external pull-up.')
    r = _one(t, "b on 14")
    assert len(r.manifest.pins) == 1


def test_unsupported_interrupt_dropped():
    t = '# from: "button on 13"\ngpio 13 is input interrupt "button" with internal pull-up.'
    r = _one(t, "button gpio13 input int pullup")
    assert r.manifest.pins[0].function == "gpio"
    assert any(a.get("source") == "normalized" for a in r.assumptions)


def test_bare_state_merged_with_later_full_line():
    text = "machine m starts in a.\nstate a: on x go to b.\nstate b.\nstate b: on y go to a.\n"
    out = nl._merge_state_lines(text)
    assert out.count("state b") == 1 and "state b: on y go to a." in out
    assert nl._merge_state_lines("machine m starts in a.\nstate a: on x go to b.\nstate b: on y go to a.\nstate b.\n").count("state b") == 1


# ---- input-bounded pins --------------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("LED on GPIO 25", {25}),
    ("relay on D25 and switch on IO26", {25, 26}),
    ("pin 32 reads a sensor, pin 33 a buzzer", {32, 33}),
    ("I2C on 21/22 with 4.7k pull-ups", {21, 22}),
    ("I2C on 21 and 22", {21, 22}),
    ("gpios 13, 14 and 15", {13, 14, 15}),
    ("LEDs on GPIO 25-27", {25, 26, 27}),
    ("LEDs on pins 25 to 27", {25, 26, 27}),
    ("pins 21 (SDA) and 22 (SCL)", {21, 22}),
    ("SDA=21, SCL=22", {21, 22}),
    ("button gpio13 input, led gpio-14", {13, 14}),
    ("pot at GPIO34", {34}),
    ("delay of 10 seconds, 5V supply, 3.3V rail", set()),
    ("blink every 10 s, a 10k resistor", set()),
    ("CS 5 ... wait, CS should be 15", {15}),
    ("LED on 25. Actually also a buzzer on 26", {25, 26}),
])
def test_gpio_numbers(text, want):
    assert nl.gpio_numbers(text) == want


def test_gpio_numbers_find_every_expected_corpus_pin():
    from pathlib import Path

    import yaml
    corpus = yaml.safe_load((Path(__file__).parent / "fixtures" / "nl" / "requests.yaml").read_text())
    n = 0
    for e in corpus:
        if e["category"] in ("clear", "rule_violation", "state_machine") and "expected_spec" in e:
            want = {p.gpio for p in cnl.parse(e["expected_spec"]).pins}
            assert want <= nl.gpio_numbers(e["request"]), e["id"]
            n += 1
    assert n >= 20


def test_translate_passes_allowed_gpios_to_grammar():
    c = Fake([sample()] * 3)
    nl.translate("LED on 25, button on 4", client=c)
    g = c.calls[0]["grammar"]
    assert 'gnum ::= "25" | "4"' in g or 'gnum ::= "4" | "25"' in g
    c = Fake(["# question: which pin?\nEND"] * 3)
    nl.translate("Something on a pin", client=c)
    assert "gnum ::= num" in c.calls[0]["grammar"]


# ---- role canonicalization -----------------------------------------------------

@pytest.mark.parametrize("roles,request_,want", [
    (["door", "door_sensor", "door_sensor"], "Pin 32 reads a door sensor", "door_sensor"),
    (["door", "door", "door_sensor"], "pin 32 reads a door sensor", "door_sensor"),
    (["door", "door_sensor"], "the door is on 32", "door"),           # longer not in the request
    (["led", "LED", "the_led"], "an LED on 25", "led"),
    (["lamp", "led", "led"], "an LED on 25", "led"),
    (["buzzer", "alarm"], "x", "buzzer"),                              # tie: first seen
])
def test_canonical_role(roles, request_, want):
    assert nl.canonical_role(roles, request_) == want


def test_role_only_difference_is_not_a_question():
    def s(role):
        return f'# from: "door sensor on 32"\ngpio 32 is input "{role}" with external pull-up.\nEND'
    r = nl.translate("Pin 32 reads a door sensor with a 10k resistor on the board",
                     client=Fake([s("door"), s("door sensor"), s("door sensor")]))
    assert r.agreed and not r.disagreements
    assert r.manifest.pins[0].role == "door_sensor"
    assert '"door sensor"' in r.spec_text


def test_real_disagreement_survives_role_canonicalization():
    def s(pull):
        return f'# from: "door on 32"\ngpio 32 is input "door" with {pull}.\nEND'
    r = nl.translate("door on 32", client=Fake([s("no pull"), s("internal pull-up"), s("no pull")]))
    assert [d["field"] for d in r.disagreements] == ["pull"]


# ---- staged state machines -----------------------------------------------------

STATEFUL = ("Button on GPIO 4 (internal pull-up). A press starts a measurement; every 10 seconds "
            "read the sensor; if 3 reads fail go to error; a press in error goes back to idle.")
PLAN_JSON = json.dumps({"machines": [{
    "name": "Sensor", "states": ["idle", "measuring", "error"], "start": "idle",
    "events": ["button_press", "read_failed"], "actions": ["read_sensor"]}]})
GOOD_FSM = ("machine sensor starts in idle.\nstate idle: on button press go to measuring.\n"
            "state measuring: every 10 s do read sensor; after 3 read failed go to error.\n"
            "state error: on button press go to idle.\nEND")
PIN4 = '# from: "Button on GPIO 4"\ngpio 4 is input "button" with internal pull-up.\nEND'


class Staged(Fake):
    """Dispatches by call kind: plan (json_schema), machine lines (fsm grammar), pins."""

    def __init__(self, plan=PLAN_JSON, fsm=GOOD_FSM, pins=PIN4):
        super().__init__([])
        self.plans, self.fsms, self.pins = ([plan] if isinstance(plan, str) else list(plan),
                                            [fsm] if isinstance(fsm, str) else list(fsm), pins)

    def complete(self, messages, grammar=None, temperature=0.7, max_tokens=0, seed=None,
                 json_schema=None):
        self.calls.append({"messages": messages, "grammar": grammar, "seed": seed,
                           "json_schema": json_schema})
        if json_schema:
            return self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
        if grammar and "machine" in grammar.split("\n", 1)[0]:
            return self.fsms.pop(0) if len(self.fsms) > 1 else self.fsms[0]
        return self.pins


def test_looks_stateful():
    from cnlp import nl_fsm
    assert nl_fsm.looks_stateful(STATEFUL)
    assert nl_fsm.looks_stateful("red for 20 s, then green for 15 s, then repeat")
    assert not nl_fsm.looks_stateful("LED on GPIO 25 and a button on GPIO 4. Wi-Fi off.")
    assert not nl_fsm.looks_stateful("I2C sensor on 21/22 with 4.7k pull-ups")


def test_looks_stateful_on_corpus():
    from pathlib import Path

    import yaml

    from cnlp import nl_fsm
    corpus = yaml.safe_load((Path(__file__).parent / "fixtures" / "nl" / "requests.yaml").read_text())
    sm = [e for e in corpus if e["category"] == "state_machine"]
    clear = [e for e in corpus if e["category"] == "clear"]
    assert sum(nl_fsm.looks_stateful(e["request"]) for e in sm) >= len(sm) - 1
    assert sum(nl_fsm.looks_stateful(e["request"]) for e in clear) <= 1


def test_validate_plan():
    from cnlp import nl_fsm
    ok, errs = nl_fsm.validate_plan(json.loads(PLAN_JSON))
    assert not errs and ok[0]["name"] == "sensor"
    assert ok[0]["events"] == ["button_press", "read_failed"]
    bad = lambda **kw: nl_fsm.validate_plan({"machines": [{**json.loads(PLAN_JSON)["machines"][0], **kw}]})[1]
    assert bad(start="nowhere")
    assert bad(states=["idle"])
    assert bad(name="go")                       # reserved word
    assert bad(states=["on", "idle"])           # reserved state
    assert bad(actions=["do_it"])               # reserved word inside an action
    dup = {"machines": [json.loads(PLAN_JSON)["machines"][0]] * 2}
    assert any("twice" in e for e in nl_fsm.validate_plan(dup)[1])
    assert nl_fsm.validate_plan({"machines": "x"})[1]


def test_plan_retries_once_then_gives_up():
    from cnlp import nl_fsm
    c = Staged(plan=["not json", PLAN_JSON])
    assert nl_fsm.plan(c, STATEFUL)[0]["name"] == "sensor"
    assert len(c.calls) == 2 and c.calls[0]["json_schema"]
    assert "Fix these problems" in c.calls[1]["messages"][-1]["content"]
    c = Staged(plan=["not json", "still not"])
    assert nl_fsm.plan(c, STATEFUL) is None and len(c.calls) == 2


def test_staged_translate_uses_planned_names_only():
    c = Staged()
    r = nl.translate(STATEFUL, client=c)
    assert r.ok and r.agreed
    sm = r.manifest.state_machines[0]
    assert sm.name == "sensor" and [s.name for s in sm.states] == ["idle", "measuring", "error"]
    kinds = [("plan" if x["json_schema"] else "fsm" if "machine" in (x["grammar"] or "").split("\n")[0]
              else "pins") for x in c.calls]
    assert kinds[0] == "plan" and kinds.count("plan") == 1
    assert kinds.count("fsm") == 3 and kinds.count("pins") == 3
    pin_grammar = next(x["grammar"] for x in c.calls if x["grammar"] and "gnum" in x["grammar"])
    assert "machine ::=" not in pin_grammar and 'gnum ::= "4"' in pin_grammar
    assert not [q for q in r.questions if "state" in q.lower()]


def test_staged_fsm_retry_fixes_missing_transition():
    broken = GOOD_FSM.replace("state error: on button press go to idle.", "state error.")
    c = Staged(fsm=[broken, GOOD_FSM])
    r = nl.translate(STATEFUL, client=c, n=1)
    retry = [x for x in c.calls if x["grammar"] and x["grammar"].startswith("root ::= \"machine")]
    assert len(retry) == 2
    assert "These problems remain" in retry[1]["messages"][-1]["content"]
    assert not [f for f in nl.fsm.check(r.manifest) if f.severity == "error"]
    assert any(t.target == "idle" for st in r.manifest.state_machines[0].states for t in st.transitions)


def test_staged_unresolved_findings_become_questions():
    broken = GOOD_FSM.replace("state error: on button press go to idle.", "state error.")
    r = nl.translate(STATEFUL, client=Staged(fsm=broken), n=1)
    assert r.ok
    assert any("'error'" in q and "no way out" in q for q in r.questions)


def test_stage1_failure_falls_back_to_single_shot():
    single = PIN4.replace("END", "") + GOOD_FSM
    c = Staged(plan=["nope", "nope"], pins=single)
    r = nl.translate(STATEFUL, client=c, n=1)
    assert r.ok and r.manifest.state_machines[0].name == "sensor"
    assert all("machine ::=" in x["grammar"] for x in c.calls if x["grammar"])


def test_non_stateful_request_skips_staging():
    c = Fake([sample()] * 3)
    nl.translate("LED on 25, button on 4", client=c)
    assert not any(x["json_schema"] for x in c.calls)


def test_request_numbers_and_sorting():
    from cnlp import nl_fsm
    assert nl_fsm.request_numbers("every 10 seconds; if 3 reads fail; fails twice; 4.7k; 3.3V") \
        == {10, 3, 2, 4}  # loose on purpose: the integer part of a decimal counts
    assert nl_fsm.request_numbers("no numbers here") == set()
    out = nl_fsm.sort_transitions("machine m starts in a.\nstate a: on x go to b; after 3 y go to b; "
                                  "every 5 s do z.\n")
    assert "state a: after 3 y go to b; every 5 s do z; on x go to b." in out
    assert out.startswith("machine m starts in a.\n")


def test_transition_order_does_not_split_votes():
    a = GOOD_FSM.replace("state measuring: every 10 s do read sensor; after 3 read failed go to error.",
                         "state measuring: after 3 read failed go to error; every 10 s do read sensor.")
    outs = [GOOD_FSM, a, GOOD_FSM]
    c = Staged(fsm=outs)
    r = nl.translate(STATEFUL, client=c)
    assert r.agreed and not r.disagreements


def test_fsm_limits_exclude_pin_numbers():
    nums, units = nl._fsm_limits("Button on GPIO 14, LED on 25. Retry every 10 seconds, give up after 3 fails.")
    assert nums == {10, 3} and units == {"s"}
    assert nl._fsm_limits("wait 2 min, then 500 ms")[1] == {"min", "ms"}
    assert nl._fsm_limits("no times given")[1] == set()


def test_resistor_pullup_without_internal_wording_is_external():
    t = '# from: "panic button on 14"\ngpio 14 is input "button" with internal pull-up.'
    r = _one(t, "panic button on GPIO 14 to ground, I have a 10k pull-up resistor on the board.")
    assert r.manifest.pins[0].pull == "external_up"
    r = _one(t, "panic button on GPIO 14 to ground, use the chip's internal pull-up, plus a 10k "
                "pull-up resistor elsewhere.")
    assert r.manifest.pins[0].pull == "internal_up"
    two = t + '\ngpio 4 is input "b2" with internal pull-up.'
    r = _one(two, "buttons on 14 and 4, a 10k pull-up resistor on the board")
    assert {p.pull for p in r.manifest.pins} == {"internal_up"}
