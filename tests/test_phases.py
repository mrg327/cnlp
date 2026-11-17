from pathlib import Path

from cnlp import phases

FIX = Path(__file__).parent / "fixtures" / "phases"
MD = (FIX / "build-procedure.md").read_text("utf-8")


def test_parse_progress_and_current():
    r = phases.parse_procedure(MD)
    assert [p["n"] for p in r["phases"]] == [0, 1, 2, 3]
    assert [p["status"] for p in r["phases"]] == ["done", "done", "not_started", "not_started"]
    assert r["current"]["n"] == 2 and r["current"]["name"] == "Matrix"
    assert r["current"]["outcome"] == "Keys read correctly"
    assert r["current"]["check"] == "all keys read; scan period 1 ms."


def test_gpios_lists_ranges_arrows_and_skips():
    p = {x["n"]: x for x in phases.parse_procedure(MD)["phases"]}
    assert p[0]["gpios"] == [2]  # Background paragraph skipped
    assert p[1]["gpios"] == [25, 26, 33]  # arrows; Common problems skipped
    assert p[2]["gpios"] == [2, 4, 14, 16, 17, 18, 19, 34, 35, 36, 39]
    assert p[3]["gpios"] == [13, 27, 32]


def test_reqs_skip_row_labels_and_q_ids():
    p = {x["n"]: x for x in phases.parse_procedure(MD)["phases"]}
    assert p[2]["reqs"] == ["F10", "R4"]


def test_unknown_status_and_all_done():
    md = MD.replace("| Done |", "| Finished-ish |", 1)
    assert phases.parse_procedure(md)["phases"][0]["status"] == "unknown"
    done = MD.replace("Not started", "Done")
    assert phases.parse_procedure(done)["current"] is None


def test_no_table():
    assert phases.parse_procedure("# nothing") == {"phases": [], "current": None}


def test_load_snapshots(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "build-procedure.md").write_text(MD)
    assert phases.load(tmp_path)["current"]["n"] == 2
    assert phases.load(tmp_path, "nope.md") is None


def test_earliest_for_gpio():
    ph = phases.parse_procedure(MD)["phases"]
    assert phases.earliest_for_gpio(ph, 2)["n"] == 0
    assert phases.earliest_for_gpio(ph, 32)["n"] == 3
    assert phases.earliest_for_gpio(ph, 21) is None
