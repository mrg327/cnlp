# ESP32 Natural-Language C Pipeline: System Design

Nov 17, 2026 · @M

This system turns a plain-English request into verified ESP32 C code. Qwen3.6-35B-A3B writes code, a scripted-fix table (later, a small Cactus Needle model) handles cheap routing, and deterministic tools verify everything they can. Four user gates cover what only a person can confirm. The user owns the spec; the system is responsible for proving the firmware matches it, and for showing exactly what evidence backs each requirement.

## At a glance

*[Diagram: pipeline from request to flash · 11 steps, 4 user gates, 1 repair loop. See the live design doc.]*

Model steps propose; deterministic tools and the four user gates decide. Every failure goes to the repair loop instead of being patched by hand.

## Components

Models supply intent and code. Deterministic tools decide what is valid.

| Component | Runs on | Job |
| --- | --- | --- |
| Qwen3.6-35B-A3B | 16 GB GPU plus system RAM for experts (already running) | Writes controlled English, code, tests and repairs; reasons about hard failures |
| Scripted-fix table (phase 1), Cactus Needle (later) | CPU | Maps structured diagnostics to scripted fixes |
| Controlled-English parser | CPU, deterministic | Parses controlled English into the pin manifest and state-machine table; renders both back |
| Pin rule checker | CPU, deterministic | Checks the manifest against chip, module and board rule layers |
| State-machine checker | CPU, deterministic | Finds unreachable states, unhandled events and dead ends |
| Code generator | CPU, deterministic | Emits pin code, driver config structs and table-driven state machines |
| Sync | CPU, deterministic | Detects hand edits and turns them into proposed spec changes |
| cdecl | CPU, deterministic | Translates declarations between English and C in both directions, after macro stripping |
| Compiler, clang-tidy or cppcheck | CPU, deterministic | Type checks, lint rules, static analysis |
| Host test builds with sanitizers and fuzzing | Host | Unit tests of the pure core; catch memory errors and undefined behavior |
| Mull or a mutation script | Host | Confirms tests fail when the code is wrong |
| CBMC, Frama-C | Host | Bounded checks and contract proofs for critical core functions |
| pytest-embedded | Host + device | On-target tests and timestamp timing over serial |
| Espressif QEMU (if the spike supports it) | Host | Host-side logic tests |
| OS sandbox | Host | Confines Pi and its bash commands to the project; no serial or eFuse access |
| Flash broker | Host, outside the sandbox | Flashes only images approved at gate 4, then captures the register readback |
| Evidence matrix | Disk | Maps every requirement sentence to its strongest evidence |
| Decision log | Disk | Records every gate approval; with the request, it is the source of truth |
| kicad-cli (later) | CPU, deterministic | Netlist cross-check, once the board has a schematic |

## Who owns what

The user owns the spec and is responsible for it being right. The system never claims the firmware is "correct"; it claims the firmware matches the user's approved spec, and shows the evidence for each requirement.

| Responsibility | Owner | How it's recorded |
| --- | --- | --- |
| What the device should do: requirements, pin plan, behavior, timing limits | User | Request, answered questions and intent-gate approval in `spec.md` and `decisions.md` |
| Whether the spec is right for the product (a wrong spec can pass every check) | User | Intent-gate sign-off; the system doesn't judge product intent |
| Hardware facts the system can't see, such as off-board pull-ups | User | Declared in the spec, approved at the hardware-risk gate |
| Accepting any requirement that has no evidence | User | Explicit acceptance at the flash gate, logged by name |
| Finding ambiguity, missing facts and contradictions in the spec | System | Clarifying questions at the intent gate |
| Rejecting what violates hardware rules | System | Rule-checker errors, which no approval can override |
| Proving, testing or measuring that the firmware matches the spec | System | Evidence matrix, regenerated on every build |
| Never overstating: every claim names its evidence | System | Evidence matrix shown at the flash gate |

In practice, approving the intent gate means "this is my spec," and approving the flash gate means "I accept this evidence, including the gaps it lists." Both approvals carry the user's own words in the decision log.

## Decisions

The first target is a classic ESP32 on a WROOM module, with no schematic, serial-only testing and no cloud services. Everything below reflects these answers.

| Question | Decision | What it changes |
| --- | --- | --- |
| Chip | Classic ESP32 | First chip rule file; S2, S3, C3 and C6 come later |
| Module | WROOM, no PSRAM | First module rule file; GPIO16/17 are usable |
| Schematic | None | Netlist cross-check is out of scope. External pull-ups are declared by the user and approved at the hardware-risk gate |
| Hand edits | Allowed, then re-translated and re-gated | The engine detects edits, renders them as controlled English, and the user approves them at the affected gate |
| Repair fallback | Ask the user only | No cloud tier; the system stays fully local |
| Test rig | Serial output only | On-target tests via `pytest-embedded`; timing uses `esp_timer` timestamps over serial, labeled as weaker evidence |
| Simulation | Try Espressif QEMU | No Wokwi. QEMU is used only for what a verify-first spike shows it emulates |
| Needle | Deferred to a later phase | Phase 1 picks scripted fixes by rule; Needle is added once the engine works |
| Rule data | Claude drafts, user reviews | Draft tables below cite Espressif sources; nothing is used until reviewed |

## Natural language first

Natural language is always the starting input. The pin manifest is an intermediate representation the system derives from it, never something a person writes by hand.

The bridge is **controlled English**: a small, strict grammar that reads like English and parses deterministically, the same trick cdecl uses for declarations.

> *User:* "There's a button on pin 4 that should wake the board, and the I2C sensor is on 21/22 with resistors on the board."
>
> *Qwen rewrites it as controlled English:* `gpio 4 is input "button" with internal pull-up, wakes from deep sleep on low` `gpio 21 is i2c0 sda with external pull-up` `gpio 22 is i2c0 scl with external pull-up`

- Qwen's only job here is free-form English → controlled English, constrained by a GBNF grammar so the output can't be malformed. This call uses no tools, because llama.cpp refuses a custom grammar together with tools.
- A deterministic parser (Lark, or a small hand-written one) turns controlled English into the manifest. No model touches that step.
- Controlled English is readable, so a person can confirm it at a glance, like reading `cdecl explain` output.

The request plus the answered gate questions are the versioned source of truth (for example `spec.md` and `decisions.md`). Everything after them is a build output. Hand edits are allowed: \`cnlp sync\` detects them, renders them as controlled English, and sends them to the affected gate for approval, so the spec and the code never drift apart.

**Scope.** Controlled English covers pins, peripheral wiring and state machines. Other behavior is still free-form code, backed by the layers in Logic correctness. The system never claims more than its evidence matrix shows, and the user owns whether the spec itself is right (see Who owns what).

## Checking the translation

The riskiest step is the first: did the system understand the English? Four mechanical checks harden it.

- **Round-trip rendering.** Render the manifest back into controlled English deterministically and diff it against Qwen's output. A mismatch is a parser or grammar bug, not a model error.
- **Traceability.** Every manifest field must cite the sentence of the request it came from. A field with no source is labeled an *assumption*, such as the internal pull-up on GPIO 4 in the example above.
- **Completeness questions instead of defaults.** The rule checker knows what a valid pin needs. A missing fact becomes a plain-English question: "Is the button active-low? Is there a resistor on the board, or should I enable the internal pull-up?"
- **Self-consistency.** Run the translation several times at nonzero temperature. Where runs disagree, the request was ambiguous, and the disagreement becomes a clarifying question.

Hardware rule violations are also explained in English: "You asked for an internal pull-up on GPIO 35, but GPIO 35 has no pull hardware. Use an external resistor, or move the button to GPIO 25?" The answer is natural language again, so the tenet holds through repairs.

## Pin manifest and hardware rules

The parser produces a manifest like this, and a plain Python checker enforces per-chip rules on it.

```yaml
chip: esp32
module: esp32-wroom-32     # module rules: no PSRAM, 3.3 V flash
board: my-board-rev-a      # board rules: what the PCB connects
idf_version: "5.5.6"       # pinned; capability macros can change between releases
features:                  # chip-wide facts that pin rules depend on (C10–C13)
  wifi: false
  bluetooth: false
  deep_sleep: true
  console: uart0           # GPIO1/3 stay reserved (C12)
  jtag: false
pins:
  - gpio: 21
    role: i2c0_sda
    mode: open_drain
    pull: external_up      # declared by the user; approved at the hardware-risk gate
  - gpio: 22
    role: i2c0_scl
    mode: open_drain
    pull: external_offboard  # pull-up lives on the sensor breakout; needs gate approval
  - gpio: 34
    role: battery
    mode: input
    function: adc          # ADC1, so C10 doesn't apply
    pull: none
  - gpio: 4
    role: button
    mode: input
    function: interrupt
    pull: internal_up
    wake: low              # ext0 wake on a low level
```

Rules come in three layers, applied in order: chip, then module, then board. A pin is usable only if every layer allows it. If the request doesn't name the module and board, the intent gate asks before anything else runs.

Chip rules for the classic ESP32:

| Rule | Result |
| --- | --- |
| GPIO 34–39 are input-only with no internal pull-ups or pull-downs | Error on output or internal pull |
| GPIO 6–11 connect to the SPI flash | Error on any use |
| Strapping pins 0, 2, 5, 12, 15 | Warning; a pull-up on GPIO 12 at boot can switch flash voltage to 1.8 V |
| ADC2 pins while Wi-Fi is on | Error |
| Input with no internal pull, external resistor or active driver | Error (floating pin) |
| Open-drain bus (I2C, 1-Wire) with no pull-up anywhere | Error |
| Deep-sleep wake pin that isn't RTC-capable | Error; its pulls must use `rtc_gpio_*` |
| Two owners for one pin | Error |

Module and board rules add reservations the chip can't know about:

| Layer | Example rule | Result |
| --- | --- | --- |
| Module | WROVER modules use GPIO16 and GPIO17 for PSRAM | Error on any use |
| Module | GPIO1 and GPIO3 are UART0 TX/RX, used by the console and flashing | Error unless the console is moved and the gate approves |
| Board | GPIO32/33 wired to a 32 kHz crystal (as on the ESP-WROVER-KIT) | Error on any use |
| Board | A pin wired to an on-board LED, button or sensor | Role must match the board file |

The S2, S3, C3 and C6 each get their own chip rule file. Module and board files are written once per hardware design and reviewed like code.

### Draft rule data: classic ESP32 + WROOM (needs review)

These tables are the first real rule files, drafted from Espressif's [ESP-IDF GPIO reference](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/gpio.html) and the [ESP32 datasheet](https://www.espressif.com/sites/default/files/documentation/esp32_datasheet_en.pdf) strapping table. Nothing is used until the user reviews it.

- [ ] Chip rules reviewed by the user
- [ ] WROOM module rules reviewed by the user

**Chip: classic ESP32.** Physical GPIOs are 0–19, 21–23, 25–27 and 32–39.

| ID | Pins | Rule | Severity |
| --- | --- | --- | --- |
| C1 | 20, 24, 28–31 | Not bonded out; any use is invalid | Error |
| C2 | 34–39 | Input only, with no internal pull-up or pull-down | Error on output or internal pull |
| C3 | 6–11 | SPI0/1 flash pins; must not be reconfigured | Error |
| C4 | 16, 17 | SPI0/1 PSRAM pins when PSRAM is present | Decided by the module layer |
| C5 | 0 | Strapping, default pull-up; low at reset enters download mode | Warning; needs gate approval |
| C6 | 2 | Strapping, default pull-down; must be low or floating for download mode | Warning; needs gate approval |
| C7 | 5 | Strapping, default pull-up; sets SDIO slave timing | Warning |
| C8 | 12 (MTDI) | Strapping, default pull-down; high at reset selects a 1.8 V flash supply | Decided by the module layer |
| C9 | 15 (MTDO) | Strapping, default pull-up; low at reset silences the boot log | Warning |
| C10 | 0, 2, 4, 12–15, 25–27 (ADC2) | ADC2 can't be used while Wi-Fi is on | Error for an ADC function with Wi-Fi on; warning for any other use of an ADC2 pin with Wi-Fi on (decided 2026-11-17) |
| C11 | 36, 39 | Don't use their interrupts with ADC, or with Wi-Fi/Bluetooth plus sleep (ECO item GPIO-3.11) | Error |
| C12 | 1 (TX), 3 (RX) | UART0, used for flashing and the console | Error unless the console is moved and the gate approves |
| C13 | 12–15 | JTAG pins | Warning if JTAG debugging is enabled |
| C14 | Wake and hold pins | Deep-sleep wake must use RTC GPIOs: 0, 2, 4, 12–15, 25–27, 32–39. Holding digital state in deep sleep needs `gpio_deep_sleep_hold_en()` | Error on a non-RTC wake pin |
| C15 | 25, 26 | Only DAC-capable pins (DAC0, DAC1) | Error on DAC elsewhere |
| C16 | All | Never rely on reset-default register values; the bootloader may change them before `app_main` | Codegen always sets every field |

**Module: ESP32-WROOM (3.3 V flash, no PSRAM).**

| ID | Pins | Rule | Severity |
| --- | --- | --- | --- |
| W1 | 6–11 | Connected to the module's integrated flash | Error |
| W2 | 16, 17 | No PSRAM, so these are ordinary GPIOs | Allowed |
| W3 | 12 (MTDI) | Flash runs at 3.3 V, so GPIO12 must be low at reset. An external pull-up, or a peripheral that drives it high at power-up, breaks boot | Error on external pull-up; warning on any input or output unless declared low at reset (decided 2026-11-17) |
| W4 | 0, 2, 5, 12, 15 | Strapping pins; anything attached must not force the wrong level at reset | Warning only when the strap level at reset isn't declared in the spec (decided 2026-11-17) |
| W5 | 37, 38 | Bonded on the chip but not brought out by the WROOM-32 module | Error (decided 2026-11-17) |

Cross-pin rules: `OWNER` (one pin, two roles: error), `OPEN_DRAIN_PULL` (open-drain or I2C line with no pull: error), `FLOATING` (GPIO or interrupt input with no pull: error), `I2C_WEAK_PULL` (I2C line relying only on the ~45 kΩ internal pull-up: warning, decided 2026-11-17).

Board rules (buttons, LEDs, USB-serial wiring) wait for the board description, since there's no schematic.

## Enforcing the plan in code

Qwen can't drift from the approved pin plan, because it never gets to write pin configuration at all.

- **Generate, don't write.** A template turns the manifest into `board_pins.h` and `board_pins_init()`. Application code refers to `PIN_BUTTON`, never to `4`.
- **Generate driver configs too.** ESP-IDF drivers configure their own pins: the I2C master config has its own `flags.enable_internal_pullup` setting, and SPI, UART and LEDC configs carry pin numbers. The generator emits these structs (for example `board_i2c0_config()`), with pins and pulls taken from the manifest. Application code passes them to the driver unchanged.
- **Lint every pin path.** A lint rule (a custom clang-tidy check or a tree-sitter query) fails if, outside generated files, code calls `gpio_config`, `gpio_set_pull_mode`, `gpio_pullup_en` or similar, or sets any `*_io_num`, `*_gpio_num` or pull field in a driver config struct.
- **Compile-time asserts as an extra layer.** The generated header carries lines like `_Static_assert(GPIO_IS_VALID_OUTPUT_GPIO(PIN_LED), "...")`. These macros check a chip-wide mask, so they can't see module or board reservations, and the SoC capability macros aren't public API. They catch typos cheaply; the layered rule checker remains the real check, and `idf_version` in the manifest pins the macros' meaning.
- **Runtime readback as the backstop.** Whatever a driver or library does at runtime is caught by comparing `gpio_dump_io_configuration()` output against the manifest (see Runtime checks).
- **Existing codebases:** parse the C with libclang or tree-sitter, extract every `gpio_config_t` initializer, driver config struct and pull or direction call, and diff against the manifest.

## Declarations and C correctness

cdecl makes the hardest part of C syntax deterministic. The compiler and analyzers cover everything else.

- **Declaration-first workflow.** Qwen writes every type, struct and prototype as cdecl English (`declare handler as pointer to function (int) returning pointer to array 3 of int`). cdecl produces the C. Signatures are locked before any function body is written.
- **Round trip.** English → `declare` → C → `explain` → English must come back unchanged, or the declaration is rejected.
- **Extraction check on clean declarations.** ESP-IDF code is full of macros such as `IRAM_ATTR`, which cdecl can't parse. The engine first strips a configured list of known attribute macros, or extracts declarations from preprocessed output (`-E`), then runs `cdecl explain` and compares the English with the stated intent. Declarations it still can't parse are reported, never silently skipped.
- **Validated tool arguments.** llama.cpp can't combine a custom GBNF grammar with tool calling (HTTP 400, verified 2026-11-17), so cdecl tool calls are constrained only by their JSON schema. The engine parses the cdecl English in each call and returns parse errors to the model.
- **Compiler gate.** The project's own toolchain build with `-Wall -Wextra -Werror` on every change.
- **Analyzers.** `idf.py clang-check` where it supports the pinned IDF version and target; otherwise `cppcheck` on the sources, plus `clang-tidy` on host builds of the logic with ESP-IDF calls stubbed out. The engine picks the path at setup and records it.
- **Sanitizers.** Host-side tests built with `-fsanitize=address,undefined`.

cdecl only covers declarations and casts. It says nothing about logic or memory safety, which is why the later checks exist.

## Schematic cross-check

**Out of scope for the first board,** which has no schematic. Until one exists, external pull-ups are declared by the user and approved at the hardware-risk gate. The rest of this section applies once a KiCad schematic is available.

Code alone can never prove a resistor is on the board. Without a schematic, `pull: external_up` is just the user's claim.

- `kicad-cli sch export netlist` produces a netlist the checker can parse.
- Power-net aliases are configured per board (`3V3`, `VCC`, `+3.3V`, `VDD_3V3`), so a pull-up is recognized whatever the net is called.
- If the manifest says `pull: external_up`, a resistor must connect that net to an aliased supply net. The resistor's value is reported, and values outside 1k–10k get a warning.
- `pull: external_offboard` covers pull-ups on breakout boards or cables that aren't in the schematic. The checker can't verify those, so each one needs explicit approval at the hardware-risk gate.
- An external pull-up with the internal pull also enabled is a warning. An input with neither is an error.
- `kicad-cli sch erc` runs in the same CI job.

This closes the loop between schematic, manifest and code.

## Runtime and simulation

Runtime checks catch anything that changes a pin after init, such as a driver, a library or a peripheral claiming it.

- **Register readback on hardware (required).** `gpio_dump_io_configuration()` prints each pin's pulls, direction and signal routing, and marks reserved pins. A test build calls it after init, the engine captures the serial output and diffs it against the manifest. This is the backstop for anything a driver or library changed, so it runs locally on every flash with no external service.
- **On-target tests over serial.** `pytest-embedded` runs tests on the board and checks serial output. This is the only test rig for now.
- **Espressif QEMU (if the spike supports it).** Used only for host-side logic tests on whatever the verify-first spike shows it emulates well. No cloud simulation is used.

## Logic correctness

The system proves conformance to the user's spec, not correctness in general. Six layers make that conformance as strong as possible, and an evidence matrix shows what each requirement actually has behind it.

**1. Generate state machines from controlled English.** Most firmware logic is modes, events and retries. The grammar is extended to cover them:

> `state idle: on button press go to measuring` `state measuring: every 10 s read sensor; after 3 failures go to error`

The engine parses this into a transition table and generates table-driven C, so Qwen never hand-writes the state machine. The table is checked mechanically for unreachable states, unhandled events and dead ends, and the user approves it in English at the intent gate.

**2. Separate a pure core from the hardware layer.** The generator enforces two layers: a pure-C core with no ESP-IDF calls, which turns inputs into decisions, and a thin hardware layer generated from the manifest. Lint fails on any ESP-IDF include in the core. Only the core needs deep verification, and on the host it gets unit tests, fuzzing and sanitizers.

**3. Tests before code.** Each requirement sentence becomes named tests, such as `test_three_sensor_failures_enter_error_state`. The user approves the test names as English sentences at the interface gate, before any implementation exists, so the model can't write tests that simply match its own code. Mutation testing (Mull, or a script that injects small bugs) then confirms the tests fail when the code is wrong.

**4. Formal checks for critical functions.** CBMC checks assertions, overflows and out-of-bounds accesses on every path up to a bound. Frama-C with ACSL contracts proves pre- and postconditions that Qwen drafts from approved English. Any counterexample becomes plain-English feedback for the repair loop. These run on small, critical core functions, such as checksums, interlocks and parsers, not on everything.

**5. Measure on real hardware.** Espressif's `pytest-embedded` runs on-target tests against serial output, which is the current test rig. Timing requirements, such as "LED toggles at 2 Hz ± 5%", are measured by logging `esp_timer` timestamps over serial. That counts as weaker evidence (`measured_serial`) because it shows when the firmware acted, not when the pin actually changed. A logic analyzer can be added later for external measurement. Static worst-case timing analysis is impractical with caches and Wi-Fi.

**6. The evidence matrix.** The engine keeps one row per requirement sentence with its strongest evidence:

| Requirement | Evidence | Status |
| --- | --- | --- |
| Button wakes the board from deep sleep | On-target test | Passed |
| Three sensor failures enter the error state | State-table check + unit test | Passed |
| Checksum rejects corrupted frames | CBMC proof | Proved |
| Display dims after 30 s | None | Needs a test or user acceptance |

The flash gate shows the matrix. A requirement with no evidence either gets a test or is accepted by name as unverified, and that acceptance goes in the decision log.

## Library preferences

Preferences go in a project profile, and checks enforce them; the prompt alone isn't trusted.

- **Allowlisted includes.** The linter fails on any header outside the list, for example ESP-IDF drivers plus chosen components, with no Arduino and no deprecated legacy drivers.
- **Banned APIs mapped to replacements.** If the old ADC driver appears, the failure message names the new API to use, so a vague preference becomes a concrete repair instruction.
- **Pinned versions** in `idf_component.yml`, so the docs fed to Qwen match what gets compiled.
- **Retrieval over library headers and examples**, so Qwen copies real signatures, not half-remembered ones.
- **New dependencies** surface at the interface gate and need explicit approval and a reason.

## Model roles and escalation

The engine parses every diagnostic into a short structured item first. Needle only maps those items to scripted fixes; Qwen handles everything that needs reasoning.

- **Engine:** turns compiler, lint and rule output into items like `{kind: missing_include, header: "driver/gpio.h", file: "main.c"}`. Raw compiler output never goes to a small model.
- **Phase 1, rules:** known item kinds map to scripted fixes, such as `add_include` or `rename_symbol`, through a lookup table. Unknown kinds go straight to Qwen.
- **Later phase, Needle:** a tool-calling model with a small context (8,192 tokens for Needle 3) replaces the lookup table for picking a scripted fix and its arguments, once the engine works and a spike shows it runs well on this machine.
- **Qwen:** writes code and handles real repairs. Run it with thinking off for cheap steps (formatting, short rewrites) and on for repairs. Keep one long session per task so thinking preservation carries context across retries.
- **Budget on 16 GB:** a Q4 quant is about 22 GB, so experts run partly in system RAM (`--n-cpu-moe`); plan for 32 GB of system RAM. Size the context for the longest repair loop (64K is a reasonable start), and use self-consistency only at the intent step, where ambiguity matters most.
- **After N failed Qwen attempts,** the system stops and asks the user in plain English. There's no cloud tier; everything stays local.
- **Failure messages are precise,** for example: "GPIO 35: internal pull-up requested, but pin has no pull hardware; use an external resistor or move to GPIO 25".

*[Diagram: repair escalation · Needle, then Qwen, then the user. See the live design doc.]*

Neither model ever declares success: every "Passes?" is the same deterministic check suite.

## Gate principles

Gates sit where a model guessed or where an action can't be undone. Everything else runs without approval.

- **Gate on inference and irreversibility, not on everything.** Deterministic steps such as parsing, code generation, compiling and linting never need approval.
- **Always show English, never raw artifacts.** Each gate shows the controlled-English rendering, the `cdecl explain` output, or a plain-language summary. The person approves intent, not code.
- **Show only what changed.** After the first approval, a gate shows a diff against the last approved version, so reviews stay short and approvals don't become automatic.
- **Approval can accept a warning but never an error.** An approved strapping-pin warning is fine. An internal pull-up on GPIO 35 stays impossible no matter who approves it.
- **Approvals are pinned to a hash of what was shown.** If anything upstream changes, dependent approvals are invalidated automatically. The system asks again only about what changed.
- **Approved items become locked context for Qwen.** The approved pin plan goes into the prompt as fixed constraints. To change it, the model must bring the change back to the gate.

## Where the gates go

Four gates run in pipeline order. The intent gate matters most, because everything downstream builds on it.

1. **Intent gate** (after natural language → controlled English). Show the controlled English, including the state-machine table. Approval means the user takes ownership of the spec. Mark every assumption, meaning any field with no source sentence in the request, and list open clarifying questions.
2. **Hardware-risk gate** (after the rule checker). Show only warnings that need sign-off: strapping pins, ADC2 together with Wi-Fi, boot-time glitches on output pins, a pull-up both on the board and enabled internally. Each comes with a one-line explanation of the risk.
3. **Interface gate** (before writing function bodies). Show the test names as English sentences, approved before any implementation exists, and public prototypes and types as `cdecl explain` English, plus any new library or component dependency and its version. Anything outside the library allowlist needs explicit approval and a reason.
4. **Flash gate** (before touching hardware). Show the evidence matrix, the final diff summary, target chip and port. Every requirement without evidence must be accepted by name. Irreversible actions get their own separate, unbatchable confirmation that spells out the consequence: burning eFuses, enabling flash encryption or secure boot, or anything else that permanently changes the chip.

## Recording approvals

Every approval is appended to `decisions.md`, or a JSON log, with:

- the timestamp
- the hash of what was shown
- the user's answer in their own words
- which gate it was

Together with the original request, the log is the source of truth. The manifest and code can be rebuilt from it at any time, and every pin decision traces back to a sentence the user wrote or a gate they approved.

To keep gates from becoming noise, track how often each gate is approved without changes. A gate that is almost always approved as-is may be asking too broadly. One that is often edited means the step before it needs work.

## Pi integration

The system ships as a Pi package that wraps a standalone pipeline engine. [Pi](https://pi.dev) is a minimal terminal coding agent with four built-in tools (read, write, edit, bash); everything else comes from TypeScript extensions.

**Split of responsibilities**

- **Pipeline engine (standalone CLI):** controlled-English parser, rule checker, code generator, check runner, decision log. It holds every guarantee and runs the same way in CI.
- **Pi package (thin front end):** registers tools and commands, renders gate dialogs, and guards generated files. It contains no rules of its own.
- **Sandbox:** Pi runs inside an OS sandbox (bubblewrap on Linux, for example through the [pi-permission-modes](https://pi.dev/packages/pi-permission-modes) package) with write access only to the project's source folders and no access to serial devices or eFuse tools. Bash commands are gated on a parsed syntax tree, not on regexes.
- **Flash broker:** flashing runs only in the engine's flash service, outside the sandbox. The service flashes only an image whose hash matches an approved gate 4 entry in the decision log, and it never burns eFuses or enables secure boot without a separate, unbatchable confirmation.
- **Model:** local Qwen, added through `~/.pi/agent/models.json` as an OpenAI-compatible provider.

**Tools registered with `pi.registerTool`**

| Tool | Engine command | What it does |
| --- | --- | --- |
| `spec_translate` | `cnlp translate` | Parses Qwen's controlled English into the manifest and state tables; returns errors, assumptions and questions |
| `cdecl_declare` | `cnlp cdecl declare` | Turns cdecl English into a C declaration |
| `cdecl_explain` | `cnlp cdecl explain` | Turns a C declaration into cdecl English, after macro stripping |
| `check_pins` | `cnlp check-pins` | Runs chip, module and board pin rules |
| `check_fsm` | `cnlp check-fsm` | Checks state tables |
| `regen` | `cnlp gen` | Regenerates pin code, driver configs and state machines |
| `run_gates` | `cnlp check` | Runs compile, lint, analyzers, host tests, mutation tests and formal checks; returns diagnostic items |
| `evidence` | `cnlp evidence` | Builds the evidence matrix |

No tool flashes. Flashing happens only through the `/flash` command, which goes through the flash gate and the broker.

**Commands registered with `pi.registerCommand`:** `/spec` starts from natural language, `/pins` shows the current plan in controlled English, `/sync` checks for hand edits and sends them to their gates, `/gate` reviews the next pending approval, and `/flash` runs the flash gate and asks the broker to flash.

**Hooks:** a `tool_call` handler returns `{ block: true, reason }` when the model tries to write or edit generated files, or the spec's decision log.

**Caveats**

- **Bash can't reach the hardware.** The sandbox has no serial devices and no write access outside the project, so the model can't flash, burn eFuses or edit the decision log, whatever it runs. The `tool_call` hook is only an early, friendlier warning.
- **Needle stays inside the engine.** A Pi session runs one model, so Needle works inside `run_gates`. For full Needle-first escalation, drive the loop from Pi's SDK mode.
- **Gates are blocking and recorded.** The engine, not Pi, checks the approval hash and writes the decision log, so a skipped or stale approval can't count.
- **Print mode has no UI.** Check `ctx.hasUI` and refuse gate commands without it.

**Extension skeleton** (`.pi/extensions/cnlp.ts`). Every tool is a thin wrapper over one `cnlp` command, so the rules live only in the engine.

```typescript
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent"; // @earendil-works/... on newer releases
import { Type } from "typebox";

const ENGINE = "cnlp";
const PROTECTED = ["board_pins.h", "board_pins.c", "board_drivers.c", "fsm_generated.c", "decisions.jsonl", "decisions.md"];
const HARDWARE_CMDS = /\b(idf\.py\b.*\bflash|esptool|espefuse)/;

// One entry per engine command. The engine holds every rule; these are wrappers.
const TOOLS = [
  { name: "spec_translate", args: (p: any) => ["translate", p.file], parameters: Type.Object({ file: Type.String() }),
    description: "Parse controlled English into the manifest and state tables. Returns parse errors, assumptions and clarifying questions." },
  { name: "cdecl_declare", args: (p: any) => ["cdecl", "declare", p.english], parameters: Type.Object({ english: Type.String() }),
    description: "Turn cdecl English (e.g. 'declare f as pointer to function (int) returning int') into C. Use for every type and prototype." },
  { name: "cdecl_explain", args: (p: any) => ["cdecl", "explain", p.c], parameters: Type.Object({ c: Type.String() }),
    description: "Turn a C declaration into cdecl English." },
  { name: "check_pins", args: () => ["check-pins"], parameters: Type.Object({}),
    description: "Run chip, module and board pin rules on the manifest." },
  { name: "check_fsm", args: () => ["check-fsm"], parameters: Type.Object({}),
    description: "Check state tables for unreachable states, unhandled events and dead ends." },
  { name: "regen", args: () => ["gen"], parameters: Type.Object({}),
    description: "Regenerate pin code, driver configs and state machines from the manifest." },
  { name: "run_gates", args: () => ["check"], parameters: Type.Object({}), timeout: 600_000,
    description: "Run compile, lint, analyzers, host tests, mutation tests and formal checks. Returns diagnostic items." },
  { name: "evidence", args: () => ["evidence"], parameters: Type.Object({}),
    description: "Build the evidence matrix: each requirement and its strongest evidence." },
];

// Shows one pending gate and records the answer. The engine checks the hash and writes the log.
async function reviewGate(pi: ExtensionAPI, ctx: any, which?: string): Promise<boolean> {
  const r = await pi.exec(ENGINE, ["gate", "next", "--json", ...(which ? ["--gate", which] : [])], { timeout: 60_000 });
  const gate = JSON.parse(r.stdout || "null");
  if (!gate) return true; // nothing pending
  const choice = await ctx.ui.select(`${gate.name}\n\n${gate.summary}`, ["Approve", "Change", "Reject"]);
  if (!choice) return false;
  const note = choice === "Approve" ? "" : (await ctx.ui.editor("Your answer, in plain English", "")) ?? "";
  const a = await pi.exec(ENGINE, ["gate", "answer", gate.id, gate.hash, choice.toLowerCase(), note], { timeout: 60_000 });
  if (a.code !== 0) ctx.ui.notify(a.stderr || "The gate changed; review it again.", "warning");
  return a.code === 0 && choice === "Approve";
}

export default function (pi: ExtensionAPI) {
  for (const t of TOOLS) {
    pi.registerTool({
      name: t.name, label: t.name, description: t.description, parameters: t.parameters,
      async execute(toolCallId, params, signal, onUpdate, ctx) {
        const r = await pi.exec(ENGINE, t.args(params), { signal, timeout: t.timeout ?? 60_000 });
        if (r.code !== 0 && !r.stdout) throw new Error(r.stderr || `${t.name} failed`);
        return { content: [{ type: "text", text: r.stdout }], details: { exitCode: r.code } };
      },
    });
  }

  // Early, friendly warnings only. The OS sandbox is the real enforcement.
  pi.on("tool_call", async (event, ctx) => {
    const input = event.input as any;
    if (event.toolName === "write" || event.toolName === "edit") {
      const path = String(input?.path ?? ""); // confirm the field name (see Verify first)
      if (PROTECTED.some((f) => path.endsWith(f))) {
        return { block: true, reason: `${path} is generated or a record. Change the spec, then call regen.` };
      }
    }
    if (event.toolName === "bash" && HARDWARE_CMDS.test(String(input?.command ?? ""))) {
      return { block: true, reason: "Flashing and eFuse tools run only through /flash and the flash broker." };
    }
  });

  pi.registerCommand("gate", {
    description: "Review the next pending approval gate",
    handler: async (args, ctx) => {
      if (!ctx.hasUI) throw new Error("Gates need the interactive UI");
      await reviewGate(pi, ctx);
    },
  });

  pi.registerCommand("flash", {
    description: "Run the flash gate, then flash the approved image through the broker",
    handler: async (args, ctx) => {
      if (!ctx.hasUI) throw new Error("The flash gate needs the interactive UI");
      if (!(await reviewGate(pi, ctx, "flash"))) return ctx.ui.notify("Not flashed: the flash gate wasn't approved.", "info");
      const r = await pi.exec(ENGINE, ["flash"], { timeout: 300_000 }); // the broker checks the approved hash itself
      ctx.ui.notify(r.code === 0 ? "Flashed; register readback matches the manifest." : r.stderr || r.stdout, r.code === 0 ? "info" : "error");
    },
  });
}
```

The skeleton follows Pi's extension docs: `execute(toolCallId, params, signal, onUpdate, ctx)` returns `{ content, details }` and throws to signal failure; a `tool_call` handler blocks by returning `{ block: true, reason }`. Pi's packages moved from the `@mariozechner` to the `@earendil-works` npm scope in 2026 ([review](https://glukhov.org/ai-devtools/pi/pi-coding-agent-review/)), so match the import to your installed version.

### How the pieces fit

*[Diagram: Pi integration architecture · thin package over a standalone engine. See the live design doc.]*

Pi supplies the loop, the model connection and the gate dialogs. The engine owns every guarantee, so CI and any future front end get the same checks.

## Interfaces

These are first drafts of every interface the engine exposes, so an implementing agent builds against one contract. The engine command is `cnlp`. All machine output is JSON on stdout; human-facing messages are plain English.

### Controlled-English grammar (pins and state machines)

```ebnf
spec        = { statement "." } ;
statement   = pin_stmt | machine_stmt | state_stmt | hw_stmt ;

pin_stmt    = "gpio" INT "is" mode [ function ] [ role ] [ pull ] [ wake ] ;
mode        = "input" | "output" | "open-drain" | periph ;
periph      = PERIPH ( "sda" | "scl" | "tx" | "rx" | "mosi" | "miso" | "sclk" | "cs" ) ;
function    = "adc" | "dac" | "interrupt" | "pwm" ;  (* "gpio 34 is input adc "battery"" *)
role        = STRING ;                                (* "button" *)
pull        = "with" ( "internal" | "external" | "off-board" ) ( "pull-up" | "pull-down" )
            | "with no pull" ;
wake        = "wakes from deep sleep on" ( "low" | "high" ) ;

machine_stmt = "machine" NAME "starts in" NAME ;     (* declares a machine; later states belong to it *)
state_stmt  = "state" NAME [ ":" transition { ";" transition } ] ;  (* no transitions: a final state *)
transition  = trigger "go to" NAME
            | "every" DURATION "do" action            (* periodic action; stays in the state *)
            | "after" INT EVENT "go to" NAME ;        (* N occurrences of EVENT in this state; reset on leaving *)
trigger     = "on" EVENT ;
action      = WORD { WORD } ;                         (* "read sensor"; ends at ";" or "." *)

hw_stmt     = "chip is" NAME | "module is" NAME | "board is" NAME
            | "gpio" INT "is" ( "high" | "low" ) "at reset"     (* strap level, rule W4 *)
            | ( "wifi" | "bluetooth" | "deep sleep" | "jtag" ) "is" ( "on" | "off" )
            | "console is on" ( "uart0" | "uart1" | "uart2" | "usb" | "nothing" ) ;

(* Tokens *)
PERIPH      = ( "i2c" | "spi" | "uart" ) DIGIT ;      (* one token: "i2c0", never "i2c 0" *)
EVENT       = WORD { WORD } ;                         (* "button press"; ends at "go to" *)
NAME        = WORD ;                                  (* lowercase identifier, may contain "_" *)
DURATION    = INT ( "ms" | "s" | "min" ) ;
(* Reserved words, never part of EVENT, NAME or an action: "go", "to", "on", "every", "after", "is", "with", "do" *)
```

Events and actions are normalised to snake_case (`button press` becomes `button_press`); durations are stored in milliseconds. A counted `after` transition fires on the Nth occurrence of its event while the machine stays in the state, and the count resets when the state is left. Example:

```
machine sensor starts in idle.
state idle: on button press go to measuring.
state measuring: every 10 s do read sensor; on read ok go to idle; after 3 read failed go to error.
state error: on button press go to idle.
```

Features default to off and the console to `uart0`. A feature the request never mentions becomes an *assumption* and a completeness question at the intent gate.

The GBNF grammar for Qwen's constrained output is generated from this file, so the two can't drift apart.

### Manifest

YAML, as in the example above, validated against a JSON Schema. Required top-level fields: `chip`, `module`, `board`, `idf_version`, `features`, `pins[]`, `state_machines[]`. `features` holds `wifi`, `bluetooth`, `deep_sleep` (booleans), `console` (`uart0`, `uart1`, `uart2`, `usb` or `none`) and `jtag` (boolean); rules C10–C13 read it. Each pin has `gpio`, `role`, `mode` and `pull` (one of `none`, `internal_up`, `internal_down`, `external_up`, `external_down`, `external_offboard`), plus optional `function` (`gpio`, `adc`, `dac`, `interrupt`, `pwm`; default `gpio`), `wake`, `reset_level` and `source` (the request sentence it came from, or `assumption`). Peripheral pins carry their bus in `role` (`i2c0_sda`), so their function is implied.

### Engine command-line interface

| Command | Does | Output |
| --- | --- | --- |
| `cnlp translate <cnl-file>` | Parses controlled English into the manifest and state tables | Manifest, or parse errors with line and column |
| `cnlp render` | Renders the manifest back to controlled English | Text, used for the round-trip check |
| `cnlp check-pins` | Runs chip, module and board rules | List of violations with rule IDs |
| `cnlp check-fsm` | Checks state tables for unreachable states, unhandled events and dead ends | List of findings |
| `cnlp gen` | Generates pin code, driver configs and state machines | List of written files with hashes |
| `cnlp cdecl declare\|explain <text>` | Wraps cdecl, with macro stripping for `explain` | C or English |
| `cnlp check` | Runs every gate check: compile, lint, analyzers, host tests, mutation tests, formal checks | Diagnostic items |
| `cnlp sync` | Detects hand edits to tracked files and renders them as controlled English | Proposed spec changes, sent to the affected gate |
| `cnlp gate next` / `cnlp gate answer <id> <hash> <choice> <note>` | Shows the next pending gate; records an answer | Gate JSON; refuses a stale hash |
| `cnlp evidence` | Builds the evidence matrix | Matrix JSON |
| `cnlp flash` | Asks the flash broker to flash the approved image, then collects register readback | Flash and readback result |

### Diagnostic item

```json
{"id": "d-0142", "source": "compiler", "kind": "missing_include",
 "file": "main/app.c", "line": 12, "symbol": "gpio_set_level",
 "message": "gpio_set_level is used but driver/gpio.h isn't included.",
 "fix": {"tool": "add_include", "args": {"header": "driver/gpio.h"}}}
```

`fix` is filled by rule in phase 1, and later by Needle. Items with no known `kind` go to Qwen.

### Decision log

Append-only JSON Lines in `decisions.jsonl`, rendered to `decisions.md` for reading:

```json
{"ts": "2026-11-17T10:20:00-08:00", "gate": "intent", "gate_id": "g-007",
 "shown_sha256": "9f2c…", "choice": "approve", "note": "Button is active-low, internal pull-up is fine.",
 "supersedes": "g-003"}
```

Each entry's hash covers exactly what the user saw. `cnlp gate answer` refuses if the current artifact's hash differs.

### Evidence matrix row

```json
{"req": "R-04", "sentence": "Three sensor failures enter the error state.",
 "evidence": [{"type": "fsm_check", "result": "pass"}, {"type": "unit_test", "name": "test_three_sensor_failures_enter_error_state", "result": "pass"}],
 "strength": "tested", "accepted_unverified": false}
```

`strength` is one of `proved`, `tested`, `measured_serial`, `none`. Rows with `none` block the flash gate unless `accepted_unverified` is true and the decision log records the acceptance.

## Build plan

Build the deterministic engine first and add models last, so every guarantee exists before anything can try to get around it. Each phase is done only when its acceptance checks pass.

| Phase | Scope | Done when |
| --- | --- | --- |
| 0. Verify first | The spike tasks in Verify first | Each spike has a recorded result, and the plan is adjusted where a spike fails |
| 1. Engine core, no models | Manifest schema, rule checker (C1–C16, W1–W4), code generation for pins and driver configs, lint rules, static asserts, decision log, flash broker with register readback | Every known-bad case below is rejected with the right rule ID. A known-good example builds with `idf.py`, flashes to a WROOM board, and its readback matches the manifest |
| 2. Controlled English and the intent gate | Grammar parser, renderer, round-trip check, traceability, completeness questions, GBNF output for Qwen, `cnlp sync` for hand edits | Round-trip is identical on all examples. A set of ambiguous requests produces clarifying questions, not guesses. A hand edit to generated code is detected and sent back to its gate |
| 3. Pi package and sandbox | Tools, commands, `tool_call` hook, gate dialogs, sandbox config, flash only through the broker | Inside the sandbox, the model can't open a serial device, can't write `decisions.jsonl`, and can't flash. A stale approval hash is refused |
| 4. Logic layers | State-machine grammar, checker and code generation; pure-core lint; tests approved before code; host tests with sanitizers; mutation testing; CBMC on marked functions; `pytest-embedded` on target; serial timestamp timing; evidence matrix at the flash gate | On an example project, every evidence-matrix row is at least `tested` or explicitly accepted. Mutation testing meets a threshold the user sets |
| 5. Repair loop | Diagnostic parser, rule-picked scripted fixes, Qwen escalation, ask the user after N failures | A set of seeded errors is either fixed or escalated with a plain-English explanation; none loop forever |
| Later | Needle, QEMU if the spike supports it, more chips and modules, schematic support | Each one re-runs all earlier acceptance checks |

### Known-bad test cases

The engine must reject every one of these, with the expected rule or check.

| Case | Expected rejection |
| --- | --- |
| Internal pull-up on GPIO35 | C2 |
| Output on GPIO34 | C2 |
| Any use of GPIO7 | C3, W1 |
| Any use of GPIO20 | C1 |
| ADC on GPIO4 with Wi-Fi enabled | C10 |
| External pull-up on GPIO12 | W3 |
| Deep-sleep wake on GPIO5 | C14 |
| Interrupt on GPIO36 with Wi-Fi and sleep | C11 |
| GPIO1 used as an LED with the console on UART0 | C12 |
| I2C bus with no pull-up anywhere | Open-drain pull rule |
| Input with no pull and no driver | Floating-input rule |
| One pin assigned two roles | Single-owner rule |
| `.flags.enable_internal_pullup = true` in app code | Driver-config lint |
| `gpio_config()` called in app code | Direct-call lint |
| ESP-IDF include in the pure core | Pure-core lint |
| State machine with an unreachable state | `cnlp check-fsm` |
| Hand edit to `board_pins.h` | `cnlp sync` detects it and sends it to a gate |
| Model runs `idf.py flash` inside the sandbox | Denied by the sandbox |
| Gate answer with a stale hash | Refused by `cnlp gate answer` |

## Verify first

These assumptions are untested. Phase 0 checks each one before the build depends on it, and records the result here.

- [ ] **cdecl on ESP-IDF code:** after stripping known attribute macros (`IRAM_ATTR` and similar), does `cdecl explain` parse the project's declarations? If not, does the preprocessed (`-E`) route work?
- [ ] **Static analysis:** does `idf.py clang-check` work with the pinned IDF version on the classic ESP32? If not, confirm the cppcheck plus host clang-tidy fallback.
- [x] **Static asserts:** do `GPIO_IS_VALID_OUTPUT_GPIO` and related macros compile inside `_Static_assert` with the pinned IDF version? *Yes on v5.5.6 (2026-11-17, `spikes/phase0a`).*
- [x] **Register readback:** does `gpio_dump_io_configuration()` exist in the pinned IDF version, and is its serial output stable enough to parse and diff? *Yes on v5.5.6: two consecutive dumps were identical. Note: a GPIO-matrix output shows `OutputEn: [periph_sig_ctrl]` with `SigOut ID: 256`, not `OutputEn: 1`; an unconfigured pin shows `InputEn: 0`. Flashing this board needs `-b 115200`; 460800 fails. Fixture: `tests/fixtures/readback/spike_phase0a.log`.*
- [ ] **Sandbox:** does the chosen sandbox (for example, `pi-permission-modes` with bubblewrap) actually block serial devices, `esptool` and `espefuse` on this machine? Try it with a deliberate attempt.
- [ ] **Pi hook fields:** which input field holds the file path in Pi's `write` and `edit` tool calls for the installed version? The skeleton assumes `path`.
- [ ] **Qwen budget:** with `--n-cpu-moe` on the 16 GB GPU, what speed and context size are usable, and how much system RAM does it need?
- [x] **Qwen grammar plus tools:** do GBNF-constrained output and tool calling work together for this model in the installed llama.cpp build? *No: "Cannot use custom grammar constraints with tools" (HTTP 400). Grammar alone works at ~114 tokens/s; it needs a terminator. See docs/PLAN.md.*
- [ ] **Espressif QEMU:** which classic ESP32 peripherals and GPIO behaviors does it emulate well enough for host-side logic tests?
- [ ] **CBMC and Frama-C:** can they process a sample pure-core function written in the project's C style?
