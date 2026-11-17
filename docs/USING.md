# Using cnlp day to day

For the ESP32 synth project (`~/projects/engr844_final_project`). You write and
review every change. cnlp only checks; it never writes your code. Output below
is trimmed from a copy of the synth at phase 4 (button matrix) of 11.

## 1. Setup (once)

```bash
cd ~/projects/cnlp
uv tool install --editable .          # puts `cnlp` on PATH
ln -sfn ~/projects/cnlp/pi/extensions/cnlp ~/.pi/agent/extensions/cnlp
ln -sfn ~/projects/cnlp/pi/skills/cnlp     ~/.pi/agent/skills/cnlp
cd ~/projects/engr844_final_project
cnlp config .                         # should print .../cnlp/projects/synth/audit.yaml
```

If `cnlp doctor` is available, run it once; it checks the same things. It does
not exist yet. The project's cnlp config lives in the cnlp repo
(`projects/synth/`), never inside your project.

## 2. A day in Pi

Start Pi from the synth directory. The extension notices the cnlp config and
tells Qwen to use the cnlp tools first. Things to ask:

| You ask | Qwen calls | You get |
| --- | --- | --- |
| "Where am I?" / "what's next?" | `cnlp_status` | phase, check, watch-for list |
| "Is my code OK?" | `cnlp_audit` | plan, code, lint, parameter findings |
| "Can I move enc_sw to GPIO 35?" | `cnlp_try` | what that change would break |
| "What should I watch for?" | `cnlp_status` | the phase's common problems |
| "Is F10 met?" / "how do I verify R4?" | `cnlp_evidence`, `cnlp_verify_plan` | status, steps for you |
| "Which rules are unreviewed?" | `cnlp_rules` | rule list |
| "What does C6 mean?" | `cnlp_ask` | answer from cnlp's own sources |

All of these call the same commands you can run yourself. Qwen sees them
through `--for-model`, a block in three parts:

```
REPORT (relay this; keep the rule IDs):      <- findings, with rule IDs
- ! C6 — GPIO 2 (probe_scan): strapping pin ...
- ✓ Real-time rules RT1–RT7: clean.
FACTS (authoritative; don't re-derive from files):   <- phase, check, watch-for
- Phase 4 of 11: Button matrix (not started); 4 of 12 phases done.
NEXT (suggest, don't do):                    <- ideas for you to decide on
- Decide: declare the reset level in spec.cnl, or move the pin.
```

Read REPORT first. FACTS are the ground truth for phase and progress. NEXT is a
list of suggestions, not work Qwen will do. If Qwen's answer disagrees with the
tool output, trust the tool output and ask again.

### Without the model: `/cnlp`

Inside Pi you can run cnlp yourself and see its normal output directly in the
transcript. The model isn't involved, so it's fast and exact:

```
/cnlp status
/cnlp audit
/cnlp try 'gpio 26 is input "enc sw" with internal pull-up.'
/cnlp verify-plan R4
/cnlp rules --needs-review
/cnlp help
```

Add `--share` to also hand the output to the model for its next turn. Commands
that record something or touch hardware (`evidence add`, `measure`, `flash`,
`gate`) are never run from `/cnlp`; it shows the exact command to run in your
shell instead.

## 3. After each change: `cnlp audit`

```bash
cd ~/projects/engr844_final_project
cnlp audit .          # human text; exit 0 clean, 1 findings with errors
```

```
✓ Audit OK — 2 warnings
Plan  2 warnings
  ! C6    GPIO 2 (probe_scan): strapping pin (default pull-down). It must be low or floating ...
  ! W4    GPIO 2 (probe_scan): strapping pin, and its level at reset isn't declared ...
Code vs plan  clean
Lint  clean
Parameters  clean
    Task scan_task is not created in the code yet (expected priority 10, core 0, ...).
Summary
  20 planned pins, 15 found in code, 7 files scanned
  5 planned pins not configured yet (expected mid-project)
```

Sections:

- **Plan**: the pin plan in `spec.cnl` against chip rules (C*), module rules
  (W*) and cross-pin rules (OWNER, FLOATING, ...).
- **Code vs plan**: your C against the plan (`AUDIT_*`): wrong pin, wrong pull,
  wrong mode, a pin used twice.
- **Lint**: includes and APIs (`LINT_*`) and the real-time rules below.
- **Parameters**: the CLAUDE.md "Key parameters" table (`PARAM_*`): sample
  rate 48000, 128 frames per buffer, 2 DMA descriptors, task priorities and
  cores, `CONFIG_FREERTOS_HZ` 1000, not unicore.

"Not yet" items (`AUDIT_UNUSED` for pins, `PARAM_TASK_MISSING` for tasks) are
things a later phase adds. They are not problems. `cnlp status` says which
phase introduces each one. `audit` works on a temp snapshot of your project
and never touches it.

### Real-time rules

| Rule | In one line |
| --- | --- |
| RT1_AUDIO_BLOCKING | The audio path never blocks, except the I2S write |
| RT2_QUEUE_TIMEOUT | Queue calls in real-time tasks use timeout 0 |
| RT3_SHARED_WRITER | A shared variable has one writer task; pass changes by queue |
| RT4_TASK_NO_BLOCK | Every task loop has a blocking call (queue wait or `vTaskDelayUntil`) |
| RT5_DOUBLE / RT5_ISR_FLOAT | Float only: no `double`, no float math in ISRs |
| RT6_STARTUP_ORDER | `app_main` creates the audio task last |
| RT7_PERIOD_DRIFT / _MISMATCH | Periodic tasks use `vTaskDelayUntil` with a `TickType_t` last-wake time, and the period matches the profile |

When Qwen proposes a fix, check it against the rule text, then run
`cnlp audit .` again. A finding is only fixed when the audit stops reporting it.

## 4. Pin changes: `cnlp try`

Before editing `spec.cnl` or code, ask what a pin change would do. It changes
nothing.

```bash
cnlp try 'gpio 35 is input "enc sw" with internal pull-up.' .
```

```
✗ This change would break 2 rules
  moves enc_sw from GPIO 27 to GPIO 35
Would introduce
  ✗ C2     GPIO 35 (enc_sw): internal pull-up or pull-down requested, but GPIO 34-39 have no pull
           hardware. ... Free pins that pass every rule for enc_sw: GPIO 27.
  ✗ OWNER  GPIO 35: assigned more than once (col_2 and enc_sw). ...
Nothing was changed; edit spec.cnl (and the code) yourself if you want it.
```

Format: `gpio <n> is input|output|open-drain "<role>" with internal pull-up |
external pull-up | off-board pull-up | no pull.` Reusing an existing role
moves that pin. The "Free pins that pass every rule" list is computed against
the whole plan, so use it instead of guessing. You then edit the plan and code
yourself and run `cnlp audit .`.

## 5. Verification and evidence

Nothing is ever recorded for you. cnlp lists the steps; you do them and record
the result.

```bash
cnlp verify-plan . F10      # steps, test names, exact commands for one requirement
cnlp verify-plan . --out ~/notes/f10.md    # printable checklist (path must be outside the project)
```

For F10 (ghosting), phase 4's requirement, the plan is 25 single-key presses
plus 400 three-corner combinations (`G1`..`G400`). Each line says which keys to
hold and which phantom key to watch. Use `--out` without a requirement ID to
get the whole checklist. When every item passes:

```bash
cnlp evidence add . F10 tested pass --value 425 --unit checks --note "25 single keys + 400 corner combos, no phantom"
```

If one fails, record a `fail` with a note naming the phantom key.

R4 (scan period) is a scope measurement. `cnlp measure` reads the bench scope
over SCPI; `--record` stores the result as evidence, and needs `--criterion`:

```bash
cnlp measure period --ch 1 --samples 20 --criterion "1 ms ±2%" --record . R4 --source-note "GPIO 2 scan probe"
```

Check the criterion against what `cnlp verify-plan . R4` says. For a
measurement you took some other way, `evidence add` takes `--value`, `--unit`
and `--criterion` (`--value 1.001ms` is split for you):

```bash
cnlp evidence add . R4 measured_scope pass --value 1.001 --unit ms --criterion "1 ms ±2%" --source "scope, GPIO 2 probe"
```

See the matrix any time with `cnlp evidence .` (one row per F*/R* requirement,
status and strongest evidence). At phase 4 it reads `0 of 17 pass, 17 missing`;
that is expected until phase 10. Evidence goes stale when the code changes, so
re-check late in the project.

## 6. Reviewing rules

The pin rules (C*, W*, ...) are drafts from the ESP32 datasheet that you are
meant to sign off.

```bash
cnlp rules --needs-review          # what is still unreviewed
cnlp rules --family chip           # one family
cnlp rules --sheet > ~/notes/rules.md   # printable review sheet with source and citation
```

Read each rule against its citation, then record your decision by editing
`src/cnlp/rules/review.yaml` yourself:

```yaml
  C2: {status: reviewed, by: "matt", date: "2026-11-20", note: "matches datasheet table"}
```

`status` is `unreviewed`, `reviewed` or `changes-requested`. cnlp only reads
this file. The Pi guard blocks the model from writing it.

## 7. What the model cannot do

The Pi extension blocks these. You run them yourself, in a normal terminal:

- `cnlp flash`, `cnlp gate review`, `cnlp gate answer`: approvals and flashing
  are decisions in your name.
- `cnlp evidence add`, `cnlp measure --record`: evidence is your claim.
- `cnlp sync --apply`: it overwrites files.
- `esptool` write/erase, `espefuse` burns, `idf.py flash`, `make flash`: the
  board is yours.
- Writing `evidence.jsonl`, `decisions.jsonl`, `review.yaml`, `components/board/`,
  `projects/` or `src/cnlp/` in the cnlp repo: those are cnlp's records and
  checker.

The guard parses the shell command (including `bash -c`, `$(...)`, pipes), so
wrapping a blocked command does not get around it. It cannot expand shell
variables or read a script the model wrote and then runs. When Qwen hits a
block, it should show you the command. Do not paste it into Pi to "approve" it.

## 8. Check-ins with Claude

Have these ready and paste them in:

```bash
cnlp status .
cnlp audit .
cnlp evidence .
```

Add `cnlp try '<line>' .` output if you are weighing a pin move, and the
`cnlp verify-plan . <REQ>` output if you are about to measure. Claude can
review and explain. You still make and defend each change.

## 9. Troubleshooting

- **Model server unreachable**: everything above works offline except
  `cnlp spec` and `cnlp ask` (and Qwen itself). Run the cnlp commands directly.
- **Pi does not start or a tool is missing**: run `pi -ne` (no extensions) to
  confirm Pi itself is fine, then add extensions back one at a time. Another
  extension breaking startup is the usual cause. `ls -l ~/.pi/agent/extensions/cnlp`
  must show the symlink into the cnlp repo.
- **"No audit config"**: you are outside the project, or the config is not
  found. Run `cnlp config .` from the synth directory. Configs live in
  `~/projects/cnlp/projects/<name>/` and the `target:` in its `audit.yaml` must
  point at your project. Do not create one inside your project. `CNLP_PROJECTS_DIR`
  overrides the config location.
- **Flashing fails**: use baud 115200 (460800 fails on this board). GPIO 2
  (`probe_scan`) is a strapping pin: nothing attached may hold it high at reset
  (rule C6), or the chip won't enter download mode.
- **Audit reports something you just fixed**: it audits a snapshot taken when
  you run it. Save the files and run `cnlp audit .` again.
