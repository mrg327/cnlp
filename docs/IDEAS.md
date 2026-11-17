# Ideas (logged, not planned)

Ideas worth keeping that are **not** on the plan. Nothing here is being built;
moving an idea into `PLAN.md` is a separate decision.

## 2026-11-17: Generalize cnlp into profiles for any programming domain

Make the system domain-general (embedded, web development, automation, ...)
by packaging everything domain-specific into **profiles** for scenarios and
environments. Each profile would bring its own:

- knowledge base (docs, rule tables, API references for `cnlp ask`)
- spec language or spec checks (controlled English for that domain)
- code checks (lint/audit rules, extractors)
- evidence types and verification methods

Profiles could compose: the synth would use an **ESP32 profile** built on
elements of a **general hardware profile** (pins, timing, real-time rules),
much as the chip → module → board rule layers already stack.

Today, the pieces that would become profile content are hard-wired:
`rules/chip`, `rules/module`, the GPIO-centric grammar in `cnl.py`, `rtlint`
real-time rules, the ESP-IDF include allowlist, and the knowledge index.
`projects/<name>/` configs (audit.yaml, cnlp-profile.yaml, requirements.yaml)
are the closest existing seed of the idea.
