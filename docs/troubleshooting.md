# Troubleshooting (Windows PowerShell unless noted)

**"running scripts is disabled on this system"** when using `.\scripts\dev.ps1`
Run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, answer `Y`, then retry.

**`python` is not found, or the wrong Python is used**
The project uses its own `.venv`. Run `.\scripts\dev.ps1 setup` first; afterwards use `.\.venv\Scripts\python`. With Python 3.12 installed, `py -3.12 -m venv .venv` also works.

**`pip install` fails during setup**
Check your internet connection, then re-run `setup`. Paste the last 15 lines of output when asking for help.

**`ingest` says "source directory data\raw not found"**
Generate the data first: `.\scripts\dev.ps1 generate`.

**A batch shows `REJECTED`**
The whole file was refused (readable? columns match the expected schema?). The `reason:` line under the table says why (`MISSING_COLUMNS`, `UNEXPECTED_COLUMNS`, `UNREADABLE_FILE`). The raw copy is kept for audit. Fix the source file and run `ingest` again; a corrected file has a new hash and is ingested normally.

**Rows are in quarantine**
Read them with `mmrisk.ingestion.read_layer("data/processed", "quarantine", "transactions")`. `_reason` explains each row and `_row_json` holds the original values.

**`reconciliation: N problem(s)`**
The files on disk disagree with the ledger (a file was deleted or edited by hand). The lake is derived data: delete the contents of `data\processed` and run `ingest` again. Never delete `data\raw` unless you intend to regenerate it.

**Rebuilding everything from scratch**
```powershell
Remove-Item -Recurse -Force data\processed\*, data\raw\*
.\scripts\dev.ps1 generate
.\scripts\dev.ps1 ingest
```
Be careful with the path: this deletes generated data only (it is git-ignored and reproducible from the seed).

**Git warns "LF will be replaced by CRLF"**
Harmless. `.gitattributes` normalises line endings in the repository.

**`quality` reports FAIL or WARN**
Run `.\.venv\Scripts\python -m mmrisk.validation check --verbose` and see the table in [data-quality.md](data-quality.md). A quarantined transaction usually also produces an orphan-label warning; read the quarantine first.

**`quality` says "no ledger"**
Run `.\scripts\dev.ps1 ingest` first.

**Rows are quarantined as `RULE:timestamp_not_future`**
The rule compares timestamps with the time of ingestion. If your computer's clock is wrong, or you generated data ending in the future, those rows are quarantined by design.

**The test suite takes minutes**
Normal: the data-quality tests build many small lakes. To run a subset: `.\.venv\Scripts\python -m pytest tests/unit -q`.

**`ModuleNotFoundError: duckdb` or `matplotlib`**
Install the EDA extra: `.\.venv\Scripts\python -m pip install -e ".[dev,eda]"` (or re-run `.\scripts\dev.ps1 setup`).

**`eda` says "lake ... not found" or "has no completed data"**
Run `.\scripts\dev.ps1 generate` and `.\scripts\dev.ps1 ingest` first.

**The report has no "Audit against ground truth" section**
The audit needs `data/raw/ground_truth/`. Pass `--source <dir>` if the extract is elsewhere. `--no-truth` skips it on purpose.

**Numbers in the report differ slightly from the committed copy**
The report reflects whatever data is in your lake. Different seeds, dates or library versions change counts; regenerate with the default settings to compare.

**`features` says "lake ... not found" or "no completed data"**
Run `.\scripts\dev.ps1 generate` then `.\scripts\dev.ps1 ingest` first.

**`python -m mmrisk.features audit` prints WARNING and exits 1**
A feature separates fraud from non-fraud almost perfectly on its own, which usually means it contains the label. Do not train on it; find which feature it is and how it was computed.

**A test says the feature catalog is out of date**
You changed `mmrisk/features/registry.py`. Regenerate the document: `.\.venv\Scripts\python -m mmrisk.features catalog`, then commit it.

**`OutOfOrderError` from the online state**
Transactions must be observed in time order per customer. A replayed or older transaction is rejected on purpose, because accepting it would change features that were already served.

**`ModuleNotFoundError: sklearn`**
Install the ML extra: `.\.venv\Scripts\python -m pip install -e ".[dev,eda,ml]"` (or re-run `.\scripts\dev.ps1 setup`).

**`train` takes a long time or the laptop becomes slow**
The full run fits 14 settings (the random forest ones dominate) and bootstraps the intervals; roughly 3 minutes on 2 cores in the sandbox, longer on a laptop. Close other programs, or use `--quick` (smaller grid, 100 trees) and `--bootstrap 100`.

**`error: train part is empty` (SplitError)**
The data is too short for a 45-day gap plus two 45-day evaluation windows plus the last 45 days. Generate a full year (the default).

**A model file will not load**
The saved models are pickles tied to the `mmrisk` code and the scikit-learn version recorded in `models/baselines/baseline_metadata.json`. Retrain with `.\scripts\dev.ps1 train` instead of copying models between machines.

**`explain` says "no risk model" or "different split"**
`explain` explains the model saved by `advanced`. Run `.\scripts\dev.ps1 advanced` first, on the same lake. If you regenerated data or changed the split, rerun `advanced`: the saved model records the split and feature version and a mismatch is refused on purpose.

**`card` says a file is missing**
The card is built from `risk_model.json`, `advanced_metrics.json` and `explain_metrics.json` in `models/advanced`. Run `advanced`, then `explain`, then `card`.

**One test is skipped ("shap")**
The cross-check against the SHAP library needs the optional extra: `.\.venv\Scripts\python -m pip install -e ".[dev,eda,ml,explain]"`.

**`advanced` says the baselines are missing or do not match**
The advanced step reuses the Phase 7 models. Run `.\scripts\dev.ps1 train` first, on the same lake. If you regenerated data, retrain: the baselines record the split and feature version and a mismatch is refused on purpose.

**`ModuleNotFoundError: lightgbm`**
Re-install the ML extra: `.\.venv\Scripts\python -m pip install -e ".[dev,eda,ml]"`.

**`advanced` takes a long time**
The full grid is 18 LightGBM settings with 6 stopping points each plus bootstraps; use `--quick --bootstrap 100` to check the setup first.

**`error: MLflow is not installed`**
Install the tracking extra: `.\.venv\Scripts\python -m pip install -e ".[dev,eda,ml,explain,tracking]"`, or run with `--no-tracking`.

**`error: only local tracking is supported`**
Remote tracking addresses (http, https, databricks, databases) are refused on purpose, see ADR-015. Use the default `sqlite:///mlflow.db` or a folder.

**Where is `mlflow.db`?**
In the folder you ran the command from. `mlflow ui` must be started from the same folder or given `--backend-store-uri sqlite:///mlflow.db`.

**The pipeline is slow**
A full run does the whole grid, bootstraps and refits. Check the setup first with `--quick --bootstrap 50`, which writes to `models\quick` and `docs\quick`.

**A run shows FAILED**
Open it with `compare` or the MLflow UI: the tag `failed_stage` names the step. Fix the cause and run again; the failed run stays as a record.

**`error: invalid cost` (exit code 2)**
A cost cannot be negative and the review effectiveness must be between 0 and 1. See `docs/cost-model.md`.
