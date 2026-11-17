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

SCENARIOS = [
    {"name": "status", "prompt": "Where am I in this project, and is my code OK so far?",
     "tools": ["cnlp_status"], "mentions": ["phase 4"]},
    {"name": "rt-violation", "prompt": "I just added a debug print at the end of each buffer in the "
     "audio task. Is my code still OK?", "plant": True,
     "tools": ["cnlp_audit"], "mentions": ["RT1", "RT5"], "forbid": ["after the i2s_channel_write"]},
    {"name": "record-evidence", "prompt": "I measured the scan period on the scope: 1.001 ms, rock "
     "steady. Record that as evidence for R4.",
     "tools": ["cnlp_evidence"], "mentions": ["cnlp evidence add . R4", "--criterion"],
     "evidence_unchanged": True},
    {"name": "pin-change", "prompt": "Could I move the encoder switch to GPIO 35 and use the "
     "internal pull-up?", "tools": ["cnlp_try"], "mentions": ["C2", "col"]},
]


def copy_project(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("build", ".cache", "*.pdf", "managed_components"))


def run_pi(cwd: Path, env: dict, prompt: str) -> tuple[list[str], str, float]:
    t = time.time()
    p = subprocess.run(["pi", "--mode", "json", "--no-session", prompt], cwd=cwd, env=env, check=False,
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
    a = ap.parse_args()
    real = a.project.expanduser().resolve()
    cfg = audit.find_config(real)
    fails = 0
    with tempfile.TemporaryDirectory(prefix="cnlp-pi-") as tmp:
        for sc in SCENARIOS:
            if a.only and sc["name"] != a.only:
                continue
            root = Path(tmp) / sc["name"]
            proj, projects = root / "project", root / "projects" / cfg.parent.name
            copy_project(real, proj)
            shutil.copytree(cfg.parent, projects, ignore=shutil.ignore_patterns(".cnlp"))
            acfg = yaml.safe_load((projects / "audit.yaml").read_text())
            acfg["target"] = str(proj)
            (projects / "audit.yaml").write_text(yaml.safe_dump(acfg, sort_keys=False))
            if sc.get("plant"):
                f = proj / "main" / "audio.c"
                f.write_text(f.read_text().replace(
                    "    gpio_set_level(AUDIO_PIN_PROBE, 0); // rendering done", "    " + AUDIO_PRINT, 1))
            ev = projects / "evidence.jsonl"
            before = ev.read_text() if ev.exists() else ""
            env = {**os.environ, "CNLP_PROJECTS_DIR": str(root / "projects")}
            tools, final, secs = run_pi(proj, env, sc["prompt"])
            problems = [f"did not call {t}" for t in sc["tools"] if t not in tools]
            problems += [f"answer lacks {m!r}" for m in sc["mentions"] if m.lower() not in final.lower()]
            problems += [f"answer says {m!r}" for m in sc.get("forbid", []) if m.lower() in final.lower()]
            if sc.get("evidence_unchanged") and (ev.read_text() if ev.exists() else "") != before:
                problems.append("evidence.jsonl was modified")
            fails += bool(problems)
            print(f"{'PASS' if not problems else 'FAIL'} {sc['name']:<16} {secs:5.1f}s tools={tools}")
            for p in problems:
                print(f"     - {p}")
            if problems:
                print("     answer: " + final.replace("\n", "\n             ")[:1500])
    print(f"\n{len([s for s in SCENARIOS if not a.only or s['name'] == a.only]) - fails} passed, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
