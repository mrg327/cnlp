# cnlp

[![CI](https://github.com/mrg327/cnlp/actions/workflows/ci.yml/badge.svg)](https://github.com/mrg327/cnlp/actions/workflows/ci.yml)

Turns a plain-English request into ESP32 C firmware and checks it against
deterministic gates. Models propose; deterministic tools and user approval
gates decide.

**Status:** MVP. The deterministic path works end to end on a WROOM-32:
controlled English → manifest → pin rules → generated C → build → gates →
flash → register readback. No model is in the loop yet (see docs/PLAN.md).

## Try it

```bash
uv sync
P=examples/blinky
uv run cnlp --project $P translate      # spec.cnl -> .cnlp/manifest.yaml
uv run cnlp --project $P check-pins     # chip + module rules
uv run cnlp --project $P gen            # components/board/
uv run cnlp --project $P gate review    # approve the intent gate interactively
(cd $P && idf.py build)                 # ESP-IDF v5.5.6 environment
uv run cnlp --project $P gate review    # flash gate shows the image hash
uv run cnlp --project $P flash          # flashes only the approved image, then diffs readback
```

Output is readable text by default. Add `--json` (before or after the subcommand) for the
raw JSON, for scripts and agents: `uv run cnlp --json --project $P check-pins`. Exit codes
are the same either way.

## Using it from Pi

```bash
ln -sfn ~/projects/cnlp/pi/extensions/cnlp ~/.pi/agent/extensions/cnlp
ln -sfn ~/projects/cnlp/pi/skills/cnlp ~/.pi/agent/skills/cnlp
```

In a project covered by a cnlp config, Pi gets read-only tools
(`cnlp_status`, `cnlp_audit`, `cnlp_evidence`, `cnlp_try`, `cnlp_ask`), and
commands that record approvals or measurements are blocked for the model.
`scripts/pi_scenarios.py <project>` re-checks the integration on temp copies.

## How it works

1. The user describes the device in plain English.
2. A local model (Qwen3.6-35B-A3B) rewrites the request as **controlled
   English**: a strict, readable grammar that parses deterministically.
3. The `cnlp` engine parses the controlled English into a pin manifest and
   state-machine tables. It checks them against chip, module and board rules,
   then generates pin code, driver configs and state machines.
4. Declarations go through **cdecl**. Code then goes through the compiler,
   linters, host tests, mutation tests and formal checks (CBMC, Frama-C).
5. Four user gates must approve the result: intent, hardware risk,
   interface, and flash. The flash gate shows an **evidence matrix**, which
   lists every requirement with its strongest evidence.
6. A flash broker outside the agent's sandbox flashes only approved images.
   It then reads back the pin registers to confirm they match the manifest.

The user owns the spec. The system's job is to prove the firmware matches
the spec, not to claim it is "correct".

## First target

- Classic ESP32 on a WROOM module (no PSRAM)
- No schematic; external pull-ups are declared and approved by the user
- Serial-only on-target testing (`pytest-embedded`)
- Fully local: no cloud models or simulators

## Docs

- [docs/design.md](docs/design.md): full system design. It covers
  interfaces, draft rule tables, the build plan, known-bad test cases and
  the verify-first list. Diagrams appear as placeholders here; they live in
  the shared design doc.
- [docs/PLAN.md](docs/PLAN.md): the working plan, broken into ordered steps.
- [docs/USING.md](docs/USING.md): daily use with Pi: what to run, what it means, what the model can't do.
- [docs/IDEAS.md](docs/IDEAS.md): ideas logged for later, not planned.

## Before building

1. Review and sign off the draft rule tables (chip rules C1–C16, WROOM
   rules W1–W4) in `docs/design.md`.
2. Run the phase 0 "Verify first" spikes.
3. Build phase 1, the engine core with no models, against the known-bad
   test cases.
