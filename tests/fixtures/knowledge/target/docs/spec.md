# Fake spec

## Functional requirements

| ID | Requirement |
| --- | --- |
| F1 | A key starts a note. |
| F2 | Notes stop without clicks. |

## Real-time requirements

| ID | Requirement | Target |
| --- | --- | --- |
| R1 | Audio meets its deadline. | Zero underruns |
| R2 | Queue is ordered. | No stuck notes |

## Verification plan

| Test | Method | Pass criterion | Covers |
| --- | --- | --- | --- |
| Headroom | Scope | Under period | R1 |
| Stress | Run it | None stuck | R2, F1, R1 |
