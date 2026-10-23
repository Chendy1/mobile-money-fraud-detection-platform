# Drift guide
Question answered: has the data the model sees moved away from what it was validated on? Design and measurements: [ADR-020](adr/ADR-020-drift-detection.md). Decisions the system made are in the [monitoring guide](monitoring.md).

```powershell
.\scripts\dev.ps1 driftref       # summarise the validation window: data\drift\reference.json
.\scripts\dev.ps1 drift          # compare the last 7 days with it: data\drift\report.md and report.json
# a window of your choice, and a demonstration of what detection looks like:
.\.venv\Scripts\python -m mmrisk.drift report --last-days 30
.\.venv\Scripts\python -m mmrisk.drift report --scale-amounts 3
```
Needs `models\advanced` and the lake (`data\processed`). The reference is written once per model; make a new one when the model changes (the report refuses a reference for another feature version). Exit code 1 means drift was detected, 2 a mistake in the command.

| Word in the report | Meaning |
|---|---|
| PSI | Population stability index of one feature: 0 means identical shares in every bin |
| Noise level | The PSI two samples of the same data are expected to show; a PSI near it is no change |
| stable / moderate / MAJOR | PSI under 0.1 / from 0.1 / from 0.25 (conventions, assumed) |
| skipped | Fewer than 500 rows, or a calendar feature in a window shorter than its cycle (31 days for `is_salary_window`, 7 for `day_of_week`) |
| calendar | Counts when scored |
| accumulating | `n_prior_txns`, `account_age_days`: grow with time by construction; shown, not counted |

`drift_detected` is true when the model's score or any counted feature is MAJOR. The per-day table shows whether a shift is sudden or slow. A large PSI says which feature moved, not why; no drift does not mean the model is right, because that needs labels.
