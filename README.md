# cnlp

Turns a plain-English request into ESP32 C firmware and checks it against
deterministic gates. Models propose; deterministic tools and user approval
gates decide.

**Status:** design only. Nothing is implemented yet.

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

## Before building

1. Review and sign off the draft rule tables (chip rules C1–C16, WROOM
   rules W1–W4) in `docs/design.md`.
2. Run the phase 0 "Verify first" spikes.
3. Build phase 1, the engine core with no models, against the known-bad
   test cases.
