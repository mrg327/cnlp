# cnlp

[![CI](https://github.com/mrg327/cnlp/actions/workflows/ci.yml/badge.svg)](https://github.com/mrg327/cnlp/actions/workflows/ci.yml)

Turns a plain-English request into ESP32 C firmware and checks it against
deterministic gates. Models propose; deterministic tools and user approval
gates decide.

**Status:** a check-first tool for an existing ESP32 project. It reads your
code and spec and reports problems; it never writes your code. It checks the
pin plan against chip and module rules, your C against the plan, real-time
rules (RT1-RT7), key parameters (sample rate, task priorities, I2C address and
clock, encoder/I2C pins), and requirement evidence. A local Qwen model in Pi
calls it as read-only tools; the checks themselves use no model. The older
generation path (below) still works for a greenfield project: controlled
English to manifest to pin rules to generated C to build, gates, flash and
register readback.

Commands, all run from the project directory (they default to `.`):

- `cnlp doctor`: check the install, config, ESP-IDF, model server and Pi setup.
- `cnlp status`: build phase, audit, real-time and evidence summary.
- `cnlp audit`: code vs pin plan, lint, real-time rules, key parameters.
- `cnlp try '<pin line>'`: what-if check of a pin change; changes nothing.
- `cnlp verify-plan [REQ]`: verification steps for a requirement.
- `cnlp evidence` / `cnlp evidence add REQ ...`: evidence matrix; record a result.
- `cnlp measure`: read the bench scope over SCPI; `--record` stores evidence.
- `cnlp rules [--sheet]`: every rule, with review status and a printable sheet.
- `cnlp spec`, `cnlp ask`: use the local model (spec drafts; questions).
- Generation path: `translate`, `check-pins`, `gen`, `gate`, `flash`, `readback`.

Daily use is in [docs/USING.md](docs/USING.md).

## Try it (generation path, example project)

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
(`cnlp_status`, `cnlp_audit`, `cnlp_evidence`, `cnlp_try`, `cnlp_verify_plan`,
`cnlp_rules`, `cnlp_ask`) and a `/cnlp <command>` slash command that shows
output without the model (`--share` also gives it to the model). Commands that
record approvals or measurements, flash, or edit cnlp's records are blocked for
the model. Run `cnlp doctor` to check the setup. `scripts/pi_scenarios.py
<project>` re-checks the integration on temp copies.

## How the generation path works

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
- [docs/USING.md](docs/USING.md): daily use with Pi and cnlp: what to run, what it means, what the model can't do.
- [docs/IDEAS.md](docs/IDEAS.md): ideas logged for later, not planned.

## Before you rely on it

Review and sign off the draft rules (`cnlp rules --sheet` prints a review
sheet; record decisions in `src/cnlp/rules/review.yaml`). The full tables are
in `docs/design.md`.
