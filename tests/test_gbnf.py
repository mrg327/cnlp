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
