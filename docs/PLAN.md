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

Not done yet: Qwen translation (next), the interface gate, tests-before-code,
Pi, sandbox. The broker runs in-process, not outside a sandbox.

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
