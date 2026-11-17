from pathlib import Path

import pytest
from lark import Lark
from lark.exceptions import LarkError

from cnlp import cnl
from cnlp.gbnf import _to_lark, grammar

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def parser():
    return Lark(_to_lark(), parser="earley", lexer="dynamic")


def accepts(parser, text: str) -> bool:
    try:
        parser.parse(text + "END")
        return True
    except LarkError:
        return False


def _specs() -> list[Path]:
    paths = sorted((ROOT / "examples").glob("*/spec.cnl"))
    paths += sorted((ROOT / "tests" / "fixtures").rglob("*.cnl"))
    return paths


def test_grammar_has_terminator_and_is_bounded():
    g = grammar()
    assert '"END"' in g
    assert "*" not in g.replace('"*"', "")  # no unbounded repetition operators
    assert "+" not in g


@pytest.mark.parametrize("path", _specs(), ids=lambda p: str(p.relative_to(ROOT)))
def test_canonical_renders_accepted(parser, path):
    try:
        m = cnl.parse(path.read_text())
    except cnl.CnlError:
        pytest.skip("not a valid spec")
    assert accepts(parser, cnl.render(m))


def test_comments_and_questions_accepted(parser):
    text = '# from: "button on 4"\n# question: Is there a resistor?\ngpio 4 is input "button" with no pull.\n'
    assert accepts(parser, text)


def test_machine_accepted(parser):
    text = (
        "machine sensor starts in idle.\n"
        "state idle: on button press go to measuring.\n"
        "state measuring: every 10 s do read sensor; after 3 read failed go to error.\n"
        "state error.\n"
    )
    assert accepts(parser, text)


def test_extras_accepted(parser):
    assert accepts(parser, "gpio 12 is low at reset.\n")
    assert accepts(parser, 'gpio 33 is input adc "level" with no pull wakes from deep sleep on high.\n')
    assert accepts(parser, "gpio 17 is uart1 tx.\n")


@pytest.mark.parametrize(
    "bad",
    [
        "gpio 4 is input.\nGPIO",  # trailing garbage
        'GPIO 4 is input "button" with no pull.\n',  # uppercase keyword
        'gpio 4 is sideways "button" with no pull.\n',
        "wifi is maybe.\n",
        "gpio 4 is input \"button\" with no pull\n",  # missing period
        "console is on uart9.\n",
        "state idle: go to x.\n",
    ],
)
def test_invalid_rejected(parser, bad):
    assert not accepts(parser, bad)


def test_renders_from_test_cnl_accepted(parser):
    from . import test_cnl

    for name in ("FULL", "BASE", "FSM"):
        m = cnl.parse(getattr(test_cnl, name))
        assert accepts(parser, cnl.render(m)), name


def test_missing_terminator_rejected(parser):
    with pytest.raises(LarkError):
        parser.parse("wifi is on.\n")


def test_allowed_gpios_restrict_pin_numbers():
    p = Lark(_to_lark(grammar({4, 25})), parser="earley", lexer="dynamic")
    ok = 'gpio 25 is output "led" with no pull.\ngpio 4 is high at reset.\n'
    assert accepts(p, ok)
    assert not accepts(p, 'gpio 5 is output "led" with no pull.\n')
    assert not accepts(p, 'gpio 2 is output "led" with no pull.\n')
    # without a restriction any pin is fine
    assert accepts(Lark(_to_lark(grammar()), parser="earley", lexer="dynamic"),
                   'gpio 5 is output "led" with no pull.\n')


def test_grammar_without_fsm_drops_machine_lines():
    p = Lark(_to_lark(grammar(fsm=False)), parser="earley", lexer="dynamic")
    assert accepts(p, 'gpio 5 is output "led" with no pull.\n')
    assert not accepts(p, "machine m starts in idle.\n")
    assert not accepts(p, "state idle.\n")


PLAN = [{"name": "sensor", "states": ["idle", "busy"], "start": "idle",
         "events": ["button_press"], "actions": ["read_sensor"]}]


def test_fsm_grammar_only_planned_names():
    from cnlp.gbnf import fsm_grammar
    p = Lark(_to_lark(fsm_grammar(PLAN)), parser="earley", lexer="dynamic")
    good = ("machine sensor starts in idle.\nstate idle: on button press go to busy.\n"
            "state busy: every 10 s do read sensor; after 3 button press go to idle.\n")
    assert accepts(p, good)
    assert cnl.parse("chip is esp32.\nmodule is esp32-wroom-32.\nboard is devkit-v1.\n" + good)
    for bad in (good.replace("go to busy", "go to error"),             # invented state
                good.replace("on button press", "on tap"),               # invented event
                good.replace("read sensor", "poll"),                    # invented action
                good.replace("machine sensor", "machine other"),        # machine name
                good.replace("state busy: every 10 s do read sensor; after 3 button press go to idle.\n",
                             "")):                                       # missing state
        assert not accepts(p, bad), bad


def test_fsm_grammar_numbers_come_from_the_request():
    from cnlp.gbnf import fsm_grammar
    p = Lark(_to_lark(fsm_grammar(PLAN, {3, 10})), parser="earley", lexer="dynamic")
    head = "machine sensor starts in idle.\nstate idle: on button press go to busy.\n"
    assert accepts(p, head + "state busy: every 10 s do read sensor.\n")
    assert accepts(p, head + "state busy: after 3 button press go to idle.\n")
    assert not accepts(p, head + "state busy: every 1 s do read sensor.\n")
    assert not accepts(p, head + "state busy: after 25 button press go to idle.\n")
    none = Lark(_to_lark(fsm_grammar(PLAN, set())), parser="earley", lexer="dynamic")
    assert accepts(none, head + "state busy: on button press go to idle.\n")
    assert not accepts(none, head + "state busy: every 10 s do read sensor.\n")


def test_fsm_grammar_timers_only_for_planned_states_with_request_numbers():
    from cnlp.gbnf import fsm_grammar
    plan = [{**PLAN[0], "timers": ["busy"]}]
    p = Lark(_to_lark(fsm_grammar(plan, {20}, {"s"})), parser="earley", lexer="dynamic")
    head = "machine sensor starts in idle.\n"
    idle = "state idle: on button press go to busy.\n"
    assert accepts(p, head + idle + "state busy: after 20 s go to idle.\n")
    assert accepts(p, head + idle + "state busy: on button press go to idle; after 20 s go to idle.\n")
    assert accepts(p, head + idle + "state busy: on button press go to idle.\n")
    assert not accepts(p, head + idle + "state busy: after 30 s go to idle.\n")      # number not in request
    assert not accepts(p, head + idle + "state busy: after 20 min go to idle.\n")    # unit not in request
    assert not accepts(p, head + idle + "state busy: after 20 s go to nowhere.\n")   # unplanned state
    assert not accepts(p, head + "state idle: after 20 s go to busy.\nstate busy: on button press go to idle.\n")
    # no unit or no number in the request: no timers at all
    for nums, units in (({20}, set()), (set(), {"s"})):
        q = Lark(_to_lark(fsm_grammar(plan, nums, units)), parser="earley", lexer="dynamic")
        assert not accepts(q, head + idle + "state busy: after 20 s go to idle.\n")
