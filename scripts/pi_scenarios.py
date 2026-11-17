"""Run cnlp's Pi integration scenarios headlessly against a temp copy of a project.

Usage: uv run python -I scripts/pi_scenarios.py <project_dir> [--only NAME]

Copies <project_dir> (minus build output) and the matching cnlp config into a temp dir,
points CNLP_PROJECTS_DIR there, and runs `pi --mode json --no-session` for each scenario,
checking which tools Pi called and what it answered. The real project is never touched.
Needs the local model server; scenarios run one at a time (the server has one slot).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from cnlp import audit

AUDIO_PRINT = 'printf("buf done %f\\n", 0.5);\n    gpio_set_level(AUDIO_PIN_PROBE, 0); // rendering done'
SCAN_TASK = """

void scan_task(void *arg) {
  for (;;) {
    scan_read_matrix();
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}
"""

# (file, old, new): replace once; old=None appends new to the file.
PLANTS = {
    "audio_print": ("main/audio.c", "    gpio_set_level(AUDIO_PIN_PROBE, 0); // rendering done",
                    "    " + AUDIO_PRINT),
    "wrong_core": ("main/audio.c", "#define AUDIO_TASK_CORE 1", "#define AUDIO_TASK_CORE 0"),
    "scan_task_delay": ("main/scan.c", None, SCAN_TASK),
}

SCENARIOS = [
    {"name": "status", "prompt": "Where am I in this project, and is my code OK so far?",
     "tools": ["cnlp_status"], "mentions": ["phase 4"]},
    {"name": "rt-violation", "prompt": "I just added a debug print at the end of each buffer in the "
     "audio task. Is my code still OK?", "plant": "audio_print",
     "tools": [["cnlp_audit", "cnlp_status"]], "mentions": ["printf", "0.5f"], "cites": ["RT1", "RT5"],
     "forbid": ["after the i2s_channel_write"]},
    {"name": "record-evidence", "prompt": "I measured the scan period on the scope: 1.001 ms, rock "
     "steady. Record that as evidence for R4.",
     "tools": ["cnlp_evidence"], "mentions": ["cnlp evidence add . R4", "--criterion"],
     "evidence_unchanged": True},
    {"name": "pin-change", "prompt": "Could I move the encoder switch to GPIO 35 and use the "
     "internal pull-up?", "tools": ["cnlp_try"], "mentions": ["col"], "cites": ["C2"]},
    {"name": "wrong-core", "prompt": "I tidied up the audio task settings. Do my task settings "
     "still match the plan?", "plant": "wrong_core",
     "tools": [["cnlp_audit", "cnlp_status"]], "mentions": ["core 1"], "cites": ["PARAM_TASK"]},
    {"name": "watch-list", "prompt": "What should I watch out for while I build the button matrix?",
     "tools": ["cnlp_status"], "mentions": ["vTaskDelayUntil"]},
    {"name": "rt7", "prompt": "I wrote the scan task for the button matrix. Is it right?",
     "plant": "scan_task_delay", "tools": [["cnlp_audit", "cnlp_status"]], "mentions": ["vTaskDelayUntil"],
     "cites": ["RT7"]},
]


def copy_project(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("build", ".cache", "*.pdf", "managed_components"))


EXTENSION = Path(__file__).resolve().parent.parent / "pi" / "extensions" / "cnlp" / "index.ts"


def run_pi(cwd: Path, env: dict, prompt: str, isolated: bool = False) -> tuple[list[str], str, float]:
    t = time.time()
    extra = ["--no-extensions", "-e", str(EXTENSION)] if isolated else []
    p = subprocess.run(["pi", "--mode", "json", "--no-session", *extra, prompt], cwd=cwd, env=env, check=False,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
    tools, final = [], ""
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "tool_execution_start":
            tools.append(e.get("toolName", ""))
        if e.get("type") == "message_end" and e.get("message", {}).get("role") == "assistant":
            for c in e["message"].get("content", []):
                if c.get("type") == "text" and c["text"].strip():
                    final = c["text"]
    return tools, final, time.time() - t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project", type=Path)
    ap.add_argument("--only")
    ap.add_argument("--repeat", type=int, default=1, help="runs per scenario")
    ap.add_argument("--isolated", action="store_true",
                    help="load only the cnlp extension (skip the user's other Pi extensions)")
    a = ap.parse_args()
    real = a.project.expanduser().resolve()
    cfg = audit.find_config(real)
    fails = 0
    with tempfile.TemporaryDirectory(prefix="cnlp-pi-") as tmp:
        runs = [(sc, i) for sc in SCENARIOS if not a.only or sc["name"] == a.only
                for i in range(a.repeat)]
        tally: dict[str, list[bool]] = {}
        cited: dict[str, list[bool]] = {}  # quoted the rule IDs (reported, not a failure)
        for sc, i in runs:
            root = Path(tmp) / f"{sc['name']}-{i}"
            proj, projects = root / "project", root / "projects" / cfg.parent.name
            copy_project(real, proj)
            shutil.copytree(cfg.parent, projects, ignore=shutil.ignore_patterns(".cnlp"))
            acfg = yaml.safe_load((projects / "audit.yaml").read_text())
            acfg["target"] = str(proj)
            (projects / "audit.yaml").write_text(yaml.safe_dump(acfg, sort_keys=False))
            if sc.get("plant"):
                rel, old, new = PLANTS[sc["plant"]]
                f = proj / rel
                text = f.read_text()
                if old is not None and old not in text:
                    raise SystemExit(f"plant {sc['plant']}: anchor not found in {rel}")
                f.write_text(text + new if old is None else text.replace(old, new, 1))
            ev = projects / "evidence.jsonl"
            before = ev.read_text() if ev.exists() else ""
            env = {**os.environ, "CNLP_PROJECTS_DIR": str(root / "projects")}
            tools, final, secs = run_pi(proj, env, sc["prompt"], a.isolated)
            if not tools and not final and secs < 3:
                raise SystemExit("pi produced nothing; it probably failed to start. Run it by hand "
                                 "to see the error, or try --isolated (only the cnlp extension).")
            # Each entry is a tool name or a list of acceptable alternatives (any one suffices).
            problems = [f"did not call {' or '.join(t) if isinstance(t, list) else t}"
                        for t in sc["tools"]
                        if not (set(t) & set(tools) if isinstance(t, list) else t in tools)]
            problems += [f"answer lacks {m!r}" for m in sc["mentions"] if m.lower() not in final.lower()]
            problems += [f"answer says {m!r}" for m in sc.get("forbid", []) if m.lower() in final.lower()]
            if sc.get("evidence_unchanged") and (ev.read_text() if ev.exists() else "") != before:
                problems.append("evidence.jsonl was modified")
            missing_cites = [c for c in sc.get("cites", []) if c.lower() not in final.lower()]
            fails += bool(problems)
            tally.setdefault(sc["name"], []).append(not problems)
            cited.setdefault(sc["name"], []).append(not missing_cites)
            note = f"  (no {', '.join(missing_cites)} cited)" if missing_cites else ""
            print(f"{'PASS' if not problems else 'FAIL'} {sc['name']:<16} {secs:5.1f}s tools={tools}{note}")
            for p in problems:
                print(f"     - {p}")
            if problems:
                print("     answer: " + final.replace("\n", "\n             ")[:1500])
        print("\nper scenario: " + ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in tally.items()))
        if cited:
            print("cited rule IDs: " + ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in cited.items()))
    print(f"{len(runs) - fails} passed, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
