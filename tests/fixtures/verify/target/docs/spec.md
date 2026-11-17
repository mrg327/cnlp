# Synthetic spec

| ID | Requirement |
| --- | --- |
| F1 | A key plays a note. |
| F10 | No ghosting with up to 3 keys. |
| R1 | Audio finishes each buffer in time. |
| R4 | Scan period is fixed. |
| R5 | UI never glitches audio. |

| Test | Method | Pass criterion | Covers |
| --- | --- | --- | --- |
| Headroom | Toggle GPIO 14; measure on the scope. | Well under buffer period | R1 |
| Scan period | Toggle a GPIO each scan. | Period stable | R4 |
| Interference | Redraw while playing; compare traces | No change | R5 |
| Ghosting | Press every 3-key combination | No phantom keys | F10 |
