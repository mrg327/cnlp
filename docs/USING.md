# Using cnlp day to day

For the ESP32 synth project (`~/projects/engr844_final_project`). You write and
review every change. cnlp only checks; it never writes your code. Output below
is trimmed from a copy of the synth at phase 4 (button matrix) of 11.

## 1. Setup (once)

```bash
cd ~/projects/cnlp
uv tool install --editable ".[scope]" --force   # puts `cnlp` on PATH (with scope support)
ln -sfn ~/projects/cnlp/pi/extensions/cnlp ~/.pi/agent/extensions/cnlp
ln -sfn ~/projects/cnlp/pi/skills/cnlp     ~/.pi/agent/skills/cnlp
cd ~/projects/engr844_final_project
cnlp doctor
```

`cnlp doctor` is read-only. It checks the install, the project config,
`spec.cnl`, ESP-IDF, the model server, Pi, the two symlinks and node:

```
✓ project config  .../cnlp/projects/synth/audit.yaml
✓ ESP-IDF         /home/matt/esp/esp-idf-v5.5.6 v5.5.6 (matches pin v5.5.6)
✓ model server    http://100.89.28.64:8080/v1, model qwen-exec listed
— serial port     /dev/ttyUSB0 not present (board unplugged?)
✓ Setup looks good
```

`✗` is a problem to fix. `—` is optional: pyvisa is only for `cnlp measure`,
the serial port only for flashing. The project's cnlp config lives in the cnlp
repo (`projects/synth/`), never inside your project.

Commands that take a project default to the current directory. From the synth
directory, `cnlp audit`, `cnlp status`, `cnlp evidence` and
`cnlp evidence add R4 ...` need no project argument.

## 2. A day in Pi

Start Pi from the synth directory. The extension notices the cnlp config and
tells Qwen to use the cnlp tools first. Things to ask:

| You ask | Qwen calls | You get |
| --- | --- | --- |
| "Where am I?" / "what's next?" | `cnlp_status` | phase, check, watch-for list |
| "Is my code OK?" | `cnlp_audit` | plan, code, lint, parameter findings |
| "Can I move enc_sw to GPIO 35?" | `cnlp_try` | what that change would break |
| "Is F10 met?" / "how do I verify R4?" | `cnlp_evidence`, `cnlp_verify_plan` | status, steps for you |
| "Which rules are unreviewed?" | `cnlp_rules` | rule list with review status |
| "What does C6 mean?" | `cnlp_ask` | answer from cnlp's own sources |

All seven Pi tools (`cnlp_status`, `cnlp_audit`, `cnlp_evidence`, `cnlp_try`,
`cnlp_verify_plan`, `cnlp_rules`, `cnlp_ask`) are read-only and run the same
commands you can. Qwen sees their output through `--for-model`
(`cnlp --for-model status`), a block in three parts:

```
REPORT (relay this; keep the rule IDs):      <- findings, with rule IDs
- ! C6 — GPIO 2 (probe_scan): strapping pin ...
- ✓ Real-time rules RT1–RT7: clean.
- ○ AUDIT_UNUSED ×5: planned pins not configured in code yet (GPIO 32, 13, ...). Expected mid-project.
FACTS (authoritative; don't re-derive from files):   <- phase, check, watch-for
- Phase 4 of 11: Button matrix (not started); 4 of 12 phases done.
- Watch for: The scan period drifts: vTaskDelay was used instead of vTaskDelayUntil.
NEXT (suggest, don't do):                    <- ideas for you to decide on
- Decide: declare the reset level in spec.cnl, or move the pin.
```

The extension also tells Qwen to call `cnlp_status` before answering about
progress, `cnlp_try` before any pin change and `cnlp_audit` after any code
change, and never to call a finding fixed until the audit stops reporting it.

Read REPORT first. FACTS are the ground truth for phase and progress. NEXT is a
list of suggestions, not work Qwen will do. If Qwen's answer disagrees with the
tool output, trust the tool output and ask again.

### Without the model: `/cnlp`

Inside Pi you can run cnlp yourself and see its normal output directly in the
transcript. The model isn't involved, so it's fast and exact:

```
/cnlp status
/cnlp audit --share
/cnlp try 'gpio 26 is input "enc sw" with internal pull-up.'
/cnlp verify-plan R4
/cnlp rules --needs-review
/cnlp help
```

Available: `status`, `audit`, `evidence`, `rules`, `verify-plan`, `try`,
`doctor`, `config`, `check-pins`, `check-fsm`, `lint`, `render`, `help`. Give
one plain command: no pipes, redirects or `$(...)`. `--share` also hands the
output to the model for its next turn. Commands that record something, touch
hardware or need the model (`evidence add`, `measure`, `flash`, `gate`, `sync`,
`spec`, `ask`) are never run from `/cnlp`; it shows the exact command to run
in your shell instead.

## 3. After each change: `cnlp audit`

```bash
cd ~/projects/engr844_final_project
cnlp audit            # human text; exit 0 clean, 1 findings with errors
```

```
✓ Audit OK — 2 warnings
Plan  2 warnings
  ! C6    GPIO 2 (probe_scan): strapping pin (default pull-down). It must be low or floating ...
  ! W4    GPIO 2 (probe_scan): strapping pin, and its level at reset isn't declared ...
Code vs plan  clean   Lint  clean
Parameters  clean
    i2c_device_config_t.device_address is not set in the code yet (expected 0x3C, ...).
    Task scan_task is not created in the code yet (expected priority 10, core 0, ...).
Summary
  20 planned pins, 15 found in code, 7 files scanned
```

Sections:
- **Plan**: the pin plan in `spec.cnl` against chip rules (C*), module rules
  (W*) and cross-pin rules (OWNER, FLOATING, ...).
- **Code vs plan**: your C against the plan (`AUDIT_*`): wrong pin, wrong pull,
  wrong mode, a pin used twice.
- **Lint**: includes and APIs (`LINT_*`) and the real-time rules below.
- **Parameters**: the CLAUDE.md "Key parameters" table (`PARAM_*`). Each
  value is checked once the code sets it:

  | Area | Expected |
  | --- | --- |
  | Audio (phases 1-3) | sample rate 48000, 128 frames per buffer, 2 DMA descriptors |
  | FreeRTOS | `CONFIG_FREERTOS_HZ` 1000, not unicore |
  | Tasks | audio 20/core 1, scan 10/core 0, ui 5/core 0, display 2/core 0 |
  | OLED (phase 8) | I2C address `0x3C` (hex compares numerically), clock 400 kHz (`scl_speed_hz` 400000) |
  | Pins (phases 7-8) | `PARAM_PIN`: the PCNT edge/level GPIOs must be the plan's `enc_a`/`enc_b`; the I2C bus SDA/SCL must be `i2c0_sda`/`i2c0_scl` |

"Not yet" items are things a later phase adds, so they are not problems:
`AUDIT_UNUSED` (pins), `PARAM_TASK_MISSING` (tasks), `PARAM_MISSING` (values,
including all the phase 7-8 ones above, until you write that code). A value
that is set but wrong is an error. A value cnlp can't work out statically is
`PARAM_UNRESOLVED`, a warning; it is never guessed. `cnlp status` says which
phase introduces each pin. `audit` works on a temp snapshot of your project
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

A finding is fixed only when `cnlp audit` stops reporting it.

## 4. Pin changes: `cnlp try`

Before editing `spec.cnl` or code, ask what a pin change would do. It changes
nothing.

```bash
cnlp try 'gpio 35 is input "enc sw" with internal pull-up.'
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
external pull-up | off-board pull-up | no pull.` Reusing a role moves that
pin. The "Free pins" list is computed against the whole plan, so use it. Then
edit the plan and code yourself and run `cnlp audit`.

## 5. Verification and evidence

Nothing is ever recorded for you. cnlp lists the steps; you do them and record
the result.

```bash
cnlp verify-plan F10        # steps, test names, exact commands for one requirement
cnlp verify-plan --out ~/notes/f10.md    # printable checklist (path must be outside the project)
```

For F10 (ghosting), phase 4's requirement, the plan is 25 single-key presses
plus 400 three-corner combinations (`G1`..`G400`). Each line says which keys to
hold and which phantom key to watch. `--out` without a
requirement ID writes the whole checklist. When every item passes:

```bash
cnlp evidence add F10 tested pass --value 425 --unit checks --note "25 single keys + 400 corner combos, no phantom"
```

If one fails, record a `fail` with a note naming the phantom key.

R4 (scan period) is a scope measurement. `cnlp measure` reads the bench scope
over SCPI; `--record` stores the result as evidence, and needs `--criterion`:

```bash
cnlp measure period --ch 1 --criterion "1 ms ±2%" --record . R4 --source-note "GPIO 2 (probe scan)"
```

The exact line, with the criterion filled in, is printed by `cnlp verify-plan R4`. For a
measurement you took some other way, `evidence add` takes `--value`, `--unit`
and `--criterion` (`--value 1.001ms` is split for you):

```bash
cnlp evidence add R4 measured_scope pass --value 1.001 --unit ms --criterion "1 ms ±2%" --source "scope, GPIO 2 probe"
```

See the matrix any time with `cnlp evidence` (one row per F*/R* requirement,
status and strongest evidence). At phase 4 it reads `0 of 17 pass, 17 missing`;
that is expected until phase 10. Evidence goes stale when the code changes, so
re-check late in the project.

### Criteria cnlp chose

Some pass criteria in `verify-plan` are cnlp's reading of the spec, not
numbers the spec states. Each prints a "criterion basis" line. Confirm them
(or change them) before you record evidence:

| Req | Criterion | Basis |
| --- | --- | --- |
| R4 | `1 ms ±2%` | spec says 1 ms, stable. The ±2% is cnlp's choice |
| R3 | `max < 6.66 ms` | spec budget 12 ms minus output delay 2 x 2.667 ms = 5.33 ms |
| R1 | `max < 1.6 ms` | 0.6 x the 2.67 ms buffer period, the spec's fixed-point threshold |

For R3, `cnlp measure delay` uses rising-to-rising edges, but the column line
falls on a press. Set the scope by hand or use `--raw-query`. If your
instructor wants other numbers, pass your own `--criterion`.

## 6. Reviewing rules

Every rule cnlp checks (pin C*/W*, cross-pin, RT*, AUDIT_*, LINT_*, PARAM_*)
is listed with a review status. The pin rules are drafts from the ESP32
datasheet that you are meant to sign off.

```bash
cnlp rules --needs-review          # what is still unreviewed
cnlp rules --family chip           # one family
cnlp rules --sheet > ~/notes/rules.md   # printable review sheet with source and citation
```

Read each rule against its citation. Record your decision in
`src/cnlp/rules/review.yaml`, either by editing it yourself or by asking
Claude to do it at a check-in:

```yaml
  C2: {status: reviewed, by: "matt", date: "2026-11-20", note: "matches datasheet table"}
```

`status` is `unreviewed`, `reviewed` or `changes-requested`. cnlp only reads
this file and never marks a rule reviewed. The Pi model is blocked from
writing it, so do not ask Qwen to.

## 7. What the model cannot do

The Pi extension blocks these. You run them yourself, in a normal terminal:

- `cnlp flash`, `cnlp gate review|answer`: approvals and flashing are in your name.
- `cnlp evidence add`, `cnlp measure --record`: evidence is your claim.
- `cnlp sync --apply`: it overwrites files.
- `esptool` write/erase, `espefuse` burns, `idf.py flash`, `make flash`.
- Writing `evidence.jsonl`, `decisions.jsonl`, `review.yaml`, `components/board/`,
  `projects/` or `src/cnlp/`: cnlp's records and checker.

The guard parses the command (including `bash -c`, `$(...)`, pipes), so
wrapping it does not help. It cannot expand shell variables or read a script
the model wrote and then runs. When Qwen hits a block, it should show you the
command. Do not paste it into Pi to "approve" it.

## 8. Check-ins with Claude

Paste in the output of `cnlp status`, `cnlp audit` and `cnlp evidence`. Add
`cnlp try '<line>'` if you are weighing a pin move, and `cnlp verify-plan <REQ>`
before you measure. Claude can review and explain, and can record your rule
review decisions. You still make and defend each change.

## 9. Specs with timers (`cnlp spec`)

Only if you use `cnlp spec` or `cnlp translate` to write state machines. A
state can leave by itself after a time:

```
state lit: after 20 s go to dark; on button press go to dark.
```

`after 20 s go to idle` is a timer (units `ms`, `s`, `min`). `after 3 read
failed go to error` is a count of events, not a timer. A duration is never also
an event. `cnlp spec` needs the model server; the result is a draft you review.

## 10. Troubleshooting

- **Start with `cnlp doctor`**: it names the broken piece and how to fix it.
- **Model server unreachable**: everything above works except `cnlp spec`,
  `cnlp ask` and Qwen itself. `status`, `audit`, `try`, `verify-plan`, `rules`,
  `evidence` and `/cnlp ...` need no model.
- **Pi does not start or a tool is missing**: run `pi -ne` (no extensions) to
  confirm Pi itself is fine, then add extensions back one at a time. Another
  extension is the usual cause. `ls -l ~/.pi/agent/extensions/cnlp` must show
  the symlink into the cnlp repo.
- **"No audit config"**: you are outside the project, or the config is not
  found. Run `cnlp config` from the synth directory. Configs live in
  `~/projects/cnlp/projects/<name>/`; the `target:` in its `audit.yaml` must
  point at your project. Never create one inside your project.
  `CNLP_PROJECTS_DIR` overrides the config location.
- **Flashing fails**: use baud 115200 (460800 fails on this board). GPIO 2
  (`probe_scan`) is a strapping pin: nothing attached may hold it high at reset
  (rule C6), or the chip won't enter download mode.
- **Audit reports something you just fixed**: it audits a snapshot taken when
  you run it. Save the files and run `cnlp audit` again.
