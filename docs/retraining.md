# Retraining guide
Question answered: should the model be retrained, and is the new one safe to put in production? Design and measurements: [ADR-021](adr/ADR-021-gated-retraining.md). All thresholds are assumptions for this simulation (`src/mmrisk/retrain/config.py`).

```powershell
# once: put the model from the advanced step into the registry as the first production model
.\scripts\dev.ps1 regboot        # models\registry\versions\v0001, production.json
.\scripts\dev.ps1 regls          # list versions and which one is in production
.\scripts\dev.ps1 retrain-check  # exit 0 no retrain needed, 1 recommended, 2 a mistake
.\scripts\dev.ps1 retrain        # candidate + gates: data\retrain\report.md and report.json; exit 0 approved, 1 rejected
# approved? promote it yourself, with a reason; undo with rollback
.\.venv\Scripts\python -m mmrisk.registry promote v0002 --reason "gates passed, report reviewed"
.\.venv\Scripts\python -m mmrisk.registry rollback --reason "why"
```
Serve the registry with `--model-dir models\registry` (API, drift and monitoring tools accept it). A running API keeps what it loaded: restart it after promote or rollback.

| Word | Meaning |
|---|---|
| champion | The version in `production.json` |
| candidate | A model trained by `retrain run` on the newest data; stage `approved` (all required gates passed) or `rejected` |
| required / advisory | A failed required gate blocks promotion; a failed advisory gate is a WARN |
| quick | `--quick` trains a small grid for a smoke test; the version cannot be promoted without `--force` |
| forced | A promotion that overrode a refusal; recorded in `history.jsonl` |

The trigger inputs: the drift report (`data\drift\report.json`, optional), the champion's age, and the number of new fraud labels. `--as-of` moves the clock of the simulation. The data gate (fraud counts) can be relaxed for small data with `--min-train-fraud`, `--min-val-fraud`, `--min-test-fraud`; `--pr-auc-margin` and `--cost-margin` set the two margins. Look at the interval printed next to each gate before moving a threshold: on 45 days of data the intervals are wide.

What passing the gates does not mean: that the new model is better in production (the gates compare two models on the same past window), or that the label delay is handled for the newest weeks (the test window is at least 45 days old by construction).
