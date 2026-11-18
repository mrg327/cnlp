# cnlp: notes for agents working on this repo

cnlp checks ESP32 / ESP-IDF firmware against a plain-English plan. Its main user
builds a class project (`~/projects/engr844_final_project`, a FreeRTOS synth) day to
day in Pi with a local Qwen model, and checks in with Claude occasionally.
Start with `docs/USING.md` (how it's used) and `docs/PLAN.md` (what's built, results).

## Hard rules

- **Never write to the synth project or any directory outside this repo.** For any
  test or experiment, copy what you need into a temp dir and run against the copy.
  Project configs live here in `projects/<name>/`; `CNLP_PROJECTS_DIR` points cnlp at
  a temp copy of them.
- **cnlp only checks the synth; it never writes its code.** The user writes and
  reviews every change (their CLAUDE.md requires it for their instructor).
- **Never record approvals, evidence or measurements, flash the board, or touch the
  scope** unless the user asks in this conversation. `evidence add`, `measure
  --record`, `gate answer`, `flash` are the user's to run.
- `src/cnlp/rules/review.yaml` is the user's review record; edit it only when they
  tell you their decision. The rule tables themselves need the user's sign-off.

## Working here

- Python via `uv` (`uv run pytest`, `uv run ruff check src tests scripts`). CI runs
  both on every push. Live-model tests are marked `llm` and deselected by default.
- Local model: llama-swap at the URL in `src/cnlp/llm.py`, **one slot** — never run
  two model-using evals or Pi sessions at once.
- Evals: `scripts/eval_translate.py`, `scripts/eval_ask.py` (run on temp copies),
  `scripts/pi_scenarios.py <project> --repeat 3` (headless Pi on temp copies).
  Re-run them yourself before trusting a reported number.
- Where the model fails, prefer deterministic scaffolding (bound outputs by the
  input, plan-then-write, canonicalize before voting, checker-paired retries) over
  prompt tuning. Guardrails belong in code (`pi/extensions/cnlp/guard.ts`).
- Stage files by name when committing; don't `git commit -a` while others edit.
