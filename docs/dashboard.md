# Dashboard guide
Question answered: what is the system doing right now, which transactions need a person, and which model is live? Design: [ADR-022](adr/ADR-022-dashboard.md). Synthetic data only; read-only; for this computer only.

```powershell
.\.venv\Scripts\python -m pip install -e ".[dashboard]"     # once; about 450 MB on Linux; Windows not measured
.\scripts\dev.ps1 simulate       # a prediction store to look at (Phase 13), if data\predictions is empty
.\scripts\dev.ps1 baseline       # optional: lets the Health tab compare with a healthy period
.\scripts\dev.ps1 dashcheck      # which files were found, without starting anything
.\scripts\dev.ps1 dashboard      # http://127.0.0.1:8501
```
Stop it with Ctrl+C. Options: `--store`, `--baseline`, `--drift-report`, `--registry`, `--retrain-dir`, `--port`, `--no-browser`. `--host` other than 127.0.0.1, localhost or ::1 is refused unless you add `--allow-external`; there is no login.

| Tab | Shows | Needs |
|---|---|---|
| Overview | Transactions, flagged share, action mix, probability histogram, volume and flagged share per window | `data\predictions` |
| Review queue | Highest-probability REVIEW and BLOCK first, filters, one transaction with its reason codes | `data\predictions` |
| Health | Phase 14 checks on the current store; the latest drift report | store; baseline and `data\drift\report.json` are optional |
| Models | Registry versions, which one is in production, saved gate results, recent changes, the last retraining check | `models\registry`, `data\retrain` |

The sidebar sets the window (30m to 1d), masks customer ids (on by default) and shows which sources were found. Refresh reloads files. Nothing updates by itself: a new part file is picked up the next time the page runs (any click, or reloading the browser tab). Freshness is measured against the newest event, not the clock. A drift report made with `--scale-amounts` is labelled as a demonstration.

What it does not do: change a model, record a review decision, write any file, or log who looked. Masking only hides ids on screen. The reason codes describe the model, not a cause.
