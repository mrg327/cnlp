"""Validate the NL evaluation corpus (schema, parseability, rule expectations)."""

from pathlib import Path

import pytest
import yaml

from cnlp import cnl, fsm, rules

PATH = Path(__file__).parent / "fixtures" / "nl" / "requests.yaml"
CATEGORIES = {"clear", "ambiguous", "rule_violation", "state_machine", "out_of_scope"}
HEADER = ("chip is esp32.", "module is esp32-wroom-32.", "board is devkit-v1.")
ENTRIES = yaml.safe_load(PATH.read_text())


def test_schema_and_unique_ids():
    ids = [e["id"] for e in ENTRIES]
    assert len(ids) == len(set(ids))
    for e in ENTRIES:
        assert e["category"] in CATEGORIES, e["id"]
        assert isinstance(e["request"], str) and e["request"].strip(), e["id"]
        assert isinstance(e.get("notes"), str), e["id"]
        cat = e["category"]
        has_spec = "expected_spec" in e
        if cat in ("clear", "state_machine"):
            assert has_spec, e["id"]
        if cat in ("ambiguous", "out_of_scope"):
            assert not has_spec, e["id"]
            assert e.get("expected_questions"), e["id"]
        if cat == "rule_violation":
            assert e.get("expected_rules"), e["id"]
        if "expected_questions" in e:
            assert all(isinstance(q, str) for q in e["expected_questions"]), e["id"]


def test_category_counts():
    cats = [e["category"] for e in ENTRIES]
    for c in CATEGORIES:
        assert cats.count(c) >= 5, c


SPECS = [e for e in ENTRIES if "expected_spec" in e]


@pytest.mark.parametrize("e", SPECS, ids=lambda e: e["id"])
def test_expected_spec_parses(e):
    lines = e["expected_spec"].splitlines()
    for h in HEADER:
        assert h in lines, e["id"]
    cnl.parse(e["expected_spec"])


@pytest.mark.parametrize("e", SPECS, ids=lambda e: e["id"])
def test_expected_spec_findings(e):
    m = cnl.parse(e["expected_spec"])
    found = {f.rule for f in rules.check(m) + fsm.check(m)}
    errors = {f.rule for f in rules.check(m) + fsm.check(m) if f.severity == "error"}
    if e["category"] == "rule_violation":
        assert set(e["expected_rules"]) <= found, (e["id"], found)
    else:
        assert not errors, (e["id"], errors)
