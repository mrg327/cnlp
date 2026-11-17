# Build procedure

## Progress

| Phase | Outcome | Status |
| --- | --- | --- |
| 0. Preparation | Toolchain ready | Done |
| 1. Wiring | Pins checked | Done |
| 2. Matrix | Keys read correctly | Not started |
| 3. Encoder | Encoder edits settings | Not started |

## Phase 0: Preparation

**Goal:** toolchain ready.

**Background.** GPIO 6–11 are flash pins; never use them. GPIO 0, 2, 5 are strapping pins.

**Steps**

1. Blink an LED on GPIO 2.

**Check:** the LED blinks.

## Phase 1: Wiring

**Goal:** wiring checked.

**Steps**

1. Amp: BCLK → GPIO 26, LRC → 25, DIN → 33.

**Common problems**

- GPIO 1 or 3 is connected to something.

**Check:** boots with everything attached.

## Phase 2: Matrix

**Goal:** every key reads correctly at a stable scan period.

**Decisions applied:** Q2 (breadboard), Q9 (active-low).

| Row | C1 |
| --- | --- |
| R1 (top) | C |

1. R1 is the top row. Connect rows R1–R5 to GPIO 16, 17, 18, 19, 4 and columns to GPIO 34–36 and 39.
2. Probe: GPIO 14 and 2.
3. Verifies F10 and R4.

**Common problems**

- A whole row never registers: that wire is not connected to its **GPIO**.
- The scan period drifts: `vTaskDelay` was used instead of `vTaskDelayUntil`;
  see [the docs](x.md) for why.
- Keys ghost: a diode is missing.

**Check:** all keys read; scan period 1 ms.

## Phase 3: Encoder

**Goal:** encoder edits settings.

1. A → GPIO 32, B → 13, SW → 27.

**Check:** every detent registers once.

## Open questions

- GPIO 40 is mentioned here and must be ignored.
