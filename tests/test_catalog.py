from cnlp import catalog, knowledge


def test_every_discovered_rule_is_in_catalog():
    ids = {r["id"] for r in catalog.rules()}
    assert set(knowledge._scan_emitted()) <= ids
    assert {"C17", "W5", "OWNER", "RT7_PERIOD_DRIFT", "FSM_DEAD_END",
            "STATUS_PHASE_DONE_PIN_UNUSED", "PARAM_TASK", "LINT_PARSE", "AUDIT_UNUSED"} <= ids


def test_families_and_fields():
    rs = catalog.rules()
    assert len({r["id"] for r in rs}) == len(rs)
    assert {r["family"] for r in rs} == set(catalog.FAMILIES)
    for r in rs:
        assert r["summary"] and r["source"], r["id"]
        assert r["severity"], r["id"]
        assert (r["review"] is not None) == r["reviewable"]
    by = {r["id"]: r for r in rs}
    assert by["OWNER"]["family"] == "cross-pin"
    assert by["W3"]["severity"] == "error/warning"
    assert by["C17"]["pins"]


def test_review_yaml_merge(monkeypatch):
    rs = {r["id"]: r for r in catalog.rules()}
    assert rs["C17"]["review"]["status"] == "unreviewed"
    assert rs["C17"]["review"]["note"] == "added 2026-11-17"
    assert rs["AUDIT_MODE"]["review"] is None
    monkeypatch.setattr(catalog, "_load_review", lambda: {
        "C1": {"status": "reviewed", "by": "matt", "date": "2026-11-18", "note": "ok"},
        "C2": {"status": "bogus"}})
    rs = {r["id"]: r for r in catalog.rules()}
    assert rs["C1"]["review"] == {"status": "reviewed", "by": "matt",
                                  "date": "2026-11-18", "note": "ok"}
    assert rs["C2"]["review"]["status"] == "unreviewed"
    assert "[x] | C1 |" in catalog.review_sheet()


def test_review_yaml_covers_all_reviewable():
    from importlib import resources

    import yaml
    data = yaml.safe_load((resources.files("cnlp") / "rules" / "review.yaml").read_text())
    want = {r["id"] for r in catalog.rules() if r["reviewable"]}
    assert set(data["reviews"]) == want
    assert all(v["status"] == "unreviewed" for v in data["reviews"].values())


def test_sheet_has_every_reviewable_rule_with_checkbox():
    sheet = catalog.review_sheet()
    assert "never marks a rule reviewed" in sheet
    for r in catalog.rules():
        if r["reviewable"]:
            assert f"| [ ] | {r['id']} |" in sheet, r["id"]
        else:
            assert f"| {r['id']} |" not in sheet


def test_format_text_filters():
    rs = catalog.rules()
    assert "C1 " in catalog.format_text(rs, "plain", family="chip")
    assert "FSM_DEAD_END" not in catalog.format_text(rs, "plain", family="chip")
    out = catalog.format_text(rs, "plain", family="fsm")
    assert "FSM_DEAD_END" in out and "C1 " not in out
    nr = catalog.format_text(rs, "plain", needs_review=True)
    assert "C17" in nr and "AUDIT_MODE" not in nr
    assert catalog.format_text(rs, "markdown", family="module").startswith("| ID |")
    assert catalog.format_text(rs, "plain", family="nope") == "No rules match."
