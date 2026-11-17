# cnlp: working plan

The design and its phases are in [design.md](design.md). This file turns
those phases into ordered steps and records the setup they depend on.

## Environment

| Item | Value | Source |
| --- | --- | --- |
| ESP-IDF | v5.5.6, at `~/esp/esp-idf-v5.5.6` | `engr844_final_project/sdkconfig` header |
| Target | `esp32` (classic), ESP32-WROOM-32, 2 MB flash, no PSRAM | same `sdkconfig` and project notes |
| Board | Connected at `/dev/ttyUSB0` | Checked 2026-11-17 |
| Engine language | Python, managed with `uv` | User decision, 2026-11-17 |

## Done before phase 0

- [x] `design.md` moved to `docs/`
- [x] Manifest gained a `features` block (Wi-Fi, Bluetooth, deep sleep,
  console, JTAG) and a per-pin `function` (`adc`, `dac`, `interrupt`,
  `pwm`), so rules C10–C13 have facts to check
- [x] Grammar gained feature and console statements, a `function` slot,
  a single `PERIPH` token (`i2c0`), and defined `EVENT`, `action` and
  reserved words
- [x] Manifest example now uses WROOM and IDF 5.5.6, with no netlist
- [ ] **User review of the rule tables C1–C16 and W1–W4.** Phase 1 tests
  are built from them.

## Phase 0a: spikes that block phase 1

Results go in the "Verify first" list in `design.md`.

- [x] A minimal ESP-IDF project builds and flashes with v5.5.6 on `/dev/ttyUSB0`
  (needs `-b 115200`)
- [x] `gpio_dump_io_configuration()` exists in v5.5.6, and its output is
  stable enough to parse (run it twice, diff the output)
- [x] `_Static_assert(GPIO_IS_VALID_OUTPUT_GPIO(n), ...)` compiles, and fails
  on an output on GPIO 34
- [ ] `idf.py clang-check` works; if not, cppcheck is the fallback

Each later spike runs just before the phase that needs it:

| Spike | Before |
| --- | --- |
| Qwen budget, GBNF plus tool calling | Phase 2: **done 2026-11-17**, see below |
| Sandbox, Pi hook fields | Phase 3 |
| cdecl on ESP-IDF code, CBMC/Frama-C, QEMU | Phase 4 |

## MVP status (2026-11-17)

Done: steps 1–4, 6–8 below, plus the controlled-English parser and renderer
from phase 2 (`cnlp translate`, `cnlp render`, round trip, assumptions).
Verified end to end on the board: the flash broker refused an unapproved
image, then flashed the approved one, and readback matched the manifest.
64 tests pass.

Since the MVP: `cnlp gate review` (interactive), `cnlp lint` (step 5,
tree-sitter), `cnlp sync` (hand edits to generated pins become proposed spec
changes), and state machines (`machine`/`state` grammar, `cnlp check-fsm`,
table-driven pure-C codegen with a host test under ASan/UBSan;
`examples/fsm_demo` builds). 113 tests pass.

Qwen translation: `cnlp spec "<request>"` (grammar-constrained, 3 samples,
retry on parse errors, `# from:` traceability). Evaluated on a 61-request
corpus (`scripts/eval_translate.py`): 47/61 on the first run; split state
lines are now merged before parsing, which took state machines from 4/10 to
9/10. Clear requests are the weak spot (7/15): invented pulls, external vs
off-board, and sometimes questions instead of statements.

Not done yet: the interface gate, tests-before-code, Pi, sandbox. The broker
runs in-process, not outside a sandbox.

## Direction (2026-11-17): help the synth project, check-first

cnlp's main job is to help `engr844_final_project` without disrupting it:
read-only checks on the user's own code, never generated code or writes there.

- `cnlp audit projects/synth`: snapshots the target's sources into a temp dir,
  extracts the pin setup from the C (tree-sitter), diffs it against
  `projects/synth/spec.cnl`, runs pin rules and the project's lint profile.
- Real-time lint (`rtlint.py`, profile `projects/synth/cnlp-profile.yaml`):
  the synth's six real-time rules from its CLAUDE.md (RT1-RT6).
- First results (on a copy, 2026-11-17): no real-time rule findings; plan and
  code agree on the 15 pins configured so far; 5 planned pins not configured
  yet (encoder, OLED; later phases); GPIO 2 strapping warnings (C6, W4).

- Evidence matrix: `cnlp evidence projects/synth` reads R1-R6 and F1-F11 and
  the verification plan from a temp copy of the synth's `docs/spec.md`;
  `projects/synth/requirements.yaml` says which evidence types count per
  requirement. Clean RT/audit checks add `static` support (never enough
  alone for timing). The user records measurements with
  `cnlp evidence add projects/synth R4 measured_scope pass --value ...`;
  nothing is recorded automatically. Status on 2026-11-17: 0/17 with
  evidence yet (expected at phase 3).

CLI output is human-readable by default; `--json` gives the raw JSON.

## Qwen spike results (2026-11-17)

Server: llama-swap at `http://100.89.28.64:8080/v1` (from `~/.pi/agent/models.json`).
Model `qwen-exec` = Qwen3.6 35B-A3B, thinking off, temp 0.7 / top_p 0.8 /
top_k 20, 120K context; `qwen-plan` is the same model with thinking on.

- **GBNF works** through llama-swap (`"grammar"` field), at about 114 tokens/s.
  The grammar needs a terminator (`"END"`) and bounded repetition, or the
  model loops until `max_tokens`. With a short system prompt and one example,
  output is well-formed and fast (under 1 s once loaded).
- **GBNF plus tools does not work:** llama.cpp returns HTTP 400 "Cannot use
  custom grammar constraints with tools." Design change: the NL to controlled
  English step is a plain grammar-constrained completion with no tools. Tool
  calls are constrained only by their JSON schema, and the engine validates
  any controlled English or cdecl English inside tool arguments (parse
  errors go back to the model).
- **Self-consistency is useful:** three runs at temp 0.7 disagreed on both
  test requests, which is the signal for a clarifying question.
- **Typical errors are caught downstream:** "button with nothing on the
  board" came out as `with no pull`, which rule FLOATING rejects, so it
  becomes a question instead of a silent default.

## Phase 1: engine core with no models

1. **Skeleton.** `uv init --package`, `src/cnlp/`, a `cnlp` entry point,
   pytest, ruff. Every command writes JSON to stdout; messages are plain
   English.
2. **Manifest.** A JSON Schema (`schema/manifest.schema.json`), a loader
   and hand-written YAML fixtures. The fixtures are for tests only; real
   manifests come from controlled English in phase 2.
3. **Rule engine.** Rules are data (`rules/chip/esp32.yaml`,
   `rules/module/esp32-wroom-32.yaml`), each with an ID, applied chip, then
   module, then board. Cross-pin rules: single owner, open-drain pull,
   floating input. `cnlp check-pins` runs them.
   **Milestone:** every pin case in the known-bad table fails with its
   expected rule ID, as a parametrized test.
4. **Code generation.** Jinja templates produce `board_pins.h`,
   `board_pins.c`, the driver config structs and the static asserts. Output
   is byte-identical across runs, and `cnlp gen` reports a hash per file.
5. **Lint.** Tree-sitter queries catch direct GPIO calls, pin and pull
   fields in driver configs, and includes outside the allowlist.
6. **Decision log.** `decisions.jsonl`, plus `cnlp gate next` and
   `cnlp gate answer`. A stale hash is refused.
7. **Known-good example.** An ESP-IDF project under `examples/` built from
   a fixture manifest with `idf.py build`.
8. **Flash broker and readback.** It flashes only an image whose hash is
   approved, then parses `gpio_dump_io_configuration()` output and diffs it
   against the manifest.
   **Phase 1 is done** when the example flashes and its readback matches.

## Phases 2–5

These follow the build plan in `design.md`. Each one is broken into steps
here when it starts, after its spikes have run.

## Pi integration (2026-11-17)

Daily workflow: the user builds the synth in Pi (local Qwen) with cnlp, with
occasional Claude check-ins.

- `pi/extensions/cnlp` (symlinked into `~/.pi/agent/extensions/cnlp`):
  registers `cnlp_status`, `cnlp_audit`, `cnlp_evidence`, `cnlp_try`,
  `cnlp_ask` as Pi tools; when the session is in a cnlp project it tells
  the model to use them first; a `tool_call` hook blocks bash commands that
  would flash, answer gates, record evidence, `measure --record` or
  `sync --apply`.
- `pi/skills/cnlp` (symlinked into `~/.pi/agent/skills/cnlp`): the same
  guidance as a skill. On its own, Qwen ignored the skill and read files by
  hand (wrong phase, 14 tool calls); with the extension it calls one cnlp
  tool and answers correctly in ~10 s.
- `cnlp try "<pin line>"`: deterministic what-if for pin changes (found the
  GPIO 35 conflict Qwen missed). `cnlp config`: which config covers a dir.
- `scripts/pi_scenarios.py <project>`: four headless scenarios on temp
  copies (status, planted RT violation, record-evidence refusal, pin
  change). Last run: all pass; the RT scenario is flaky (sometimes omits
  rule IDs).
- `evidence add` now splits `1.001ms` into value and unit and requires
  `--criterion` for measurements.

Pin suggestions (2026-11-17): rule messages name free pins computed from
the whole plan (each candidate tried and rechecked), with a warnings-only
fallback. New rule C17 (ADC on a pin with no ADC channel) needs review.
Pi must be run with stdin closed in headless tests.

## Pi reliability (2026-11-17)

`scripts/pi_scenarios.py --repeat 3 --isolated`: 7 scenarios (status,
planted RT1/RT5, evidence refusal, pin change, planted wrong task core,
phase watch-list, planted RT7) x 3 runs: **21/21 substantively correct**,
4-8 s each; rule IDs quoted in 19/21 (PARAM_TASK is often paraphrased).
Fixes that got it there: scoring substance separately from citations, and
naming "what to watch for / do next" questions in the extension's guidance
so Qwen uses the status watch-list instead of reading the procedure.

`--isolated` loads only the cnlp extension (useful when another extension
breaks Pi's startup, as pi-subagents 0.89.0's config change did briefly on
2026-11-17). With the user's full extension set, all 7 scenarios pass.

## cnlp ask and the knowledge index (2026-11-17)

- Every rule ID any module emits gets an indexed chunk, found by scanning the
  source (a test enforces it); every command has a chunk; project key
  parameters and per-phase watch-lists are indexed.
- Pin-move questions are answered with a concrete `cnlp try '<pin line>'`
  built from the plan and validated before it is shown; prompt-injection
  questions ("ignore your rules", "run cnlp flash", "write scan_task") are
  refused without calling the model.
- `cnlp audit`, `cnlp status` and `cnlp evidence [add]` default to the
  project in the current directory.
- Eval (54 questions, scratchpad copy of the synth): 42/54 before, 53/54
  after (measured by me on a fresh copy), 27/27 answers fully grounded.
  The one miss was a correct `cnlp status` suggestion where an answer was
  expected; state questions may now be answered either way.

## Translator: clear requests (2026-11-17)

Deterministic normalization of each sample before voting (each change is
listed as an assumption): invented pulls on outputs dropped, "on the
breakout/module" pulls become off-board, interrupt/wake wording mapped,
`deep sleep is on` added for wake pins, duplicates removed; bare
`state X.` lines merged; one retry when a sample only asks questions about
pins the request plainly names. Eval: clear 7/15 -> 14/15, total 52/61 ->
58/61 (runs: 56, 56, 58); state machines 8/10 (baseline 9/10, swing 7-9).
Remaining: a spurious SPI pin (clear-014); state machines with missing
transitions or invented states; role-wording disagreements between samples.

## Translator scaffolds (2026-11-17)

Following "where the model falls apart, scaffold or decompose": GPIO numbers
in the grammar are limited to those the request mentions; state machines are
planned first (JSON-schema call: names of machines, states, events, actions,
checked deterministically) and then written under a grammar generated from
that plan, so invented names are impossible; counts/periods/units are
limited to values in the request; transitions are sorted and roles
canonicalized before voting; each retry is paired with a checker.
Eval (verified independently): 61/61 pass; agreement 26/61 -> 43/61
(state machines 2/10 -> 5/10). Gap: the language has no time-based
transition ("after 20 s go to ..."); timeouts are modeled as event counts.
