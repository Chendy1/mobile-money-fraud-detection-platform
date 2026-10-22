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

**`error: the API needs its extra`**
Install it: `.\.venv\Scripts\python -m pip install -e ".[dev,eda,ml,explain,tracking,api]"`.

**`error: no model at models\advanced\risk_model.joblib`**
Run `.\scripts\dev.ps1 advanced` (after `train`) or `.\scripts\dev.ps1 pipeline` first. A `--quick` pipeline run saves its model in `models\quick\advanced`; pass `--model-dir models\quick\advanced`.

**`error: model was built for feature version ...`**
The saved model and the code disagree on the feature set. Retrain.

**API returns 409 `out_of_order`**
The transaction is not later than the customer's last one in the replayed history (or already scored). Use a later timestamp, or start the server with `--history-until` set before the time you want to score.

**API returns 404 `unknown_customer`**
Use an id from `data\processed` cleaned customers (for example `C000001`).

**`error: refusing to listen on ...`**
The API has no authentication, so only 127.0.0.1 is allowed by default. `--allow-external` overrides that; do not use it on a shared network.

**A `StarletteDeprecationWarning` about httpx during tests**
Harmless: newer Starlette prefers `httpx2` for its test client. The tests work with either.

**Docker uses too much memory on an 8 GB machine**
Cap WSL2: create or edit `%UserProfile%\.wslconfig` with
```
[wsl2]
memory=3GB
processors=2
```
then run `wsl --shutdown` and start Docker Desktop again. Stop containers you do not need (`docker ps`, `docker stop <name>`) before `dev.ps1 up`.

**`docker compose up` says the Docker engine is not running**
Start Docker Desktop and wait until it says it is running, then retry.

**The container is `starting` for a long time, then `unhealthy`**
The API replays the whole history before it is ready (about 200,000 transactions take seconds natively, longer through Windows bind mounts). The health check waits up to two minutes. Read the log with `docker compose logs api`.

**Log says `no model at /app/models/advanced/risk_model.joblib`**
`models\advanced` is missing or empty on the host (Docker creates an empty folder for a missing mount). Run `.\scripts\dev.ps1 advanced` or `pipeline` first. A `--quick` pipeline run saves to `models\quick\advanced`: edit the first `volumes:` line in `docker-compose.yml` to use that folder.

**Log says the lake has no completed transactions**
`data\processed` must be the lake built by the ingestion step (it contains `cleaned`).

**Container exits with code 137**
It ran out of memory. Raise `mem_limit` in `docker-compose.yml` (about 1.2 KB per replayed transaction plus 290 MB) or replay less with `--history-until`.

**`Read-only file system` in the log**
The container runs with a read-only root filesystem and a writable `/tmp`. If something needs another writable path, add a `tmpfs:` entry for it and report it, because the assistant could not run the container to check this.

**`python -m mmrisk.streaming run` says the store already has predictions (exit 2)**
A second run would mix two replays. Use another `--store`, delete the folder, or add `--append` on purpose.

**Streaming over HTTP: many `out_of_order`, `conflict` or `duplicate` outcomes**
The API keeps what it has scored in memory, so it must be restarted before the same events are sent again, and it must have replayed exactly the history before `--from` (`serve --history-until <same time>`). A stream that starts earlier than the API's history looks like a flood of repeats.

**`--from is required with --url`**
In Docker Compose set `STREAM_FROM` first (`$env:STREAM_FROM = "2025-12-24T00:00:00"`); empty means no start time.

**The simulator cannot write `data/predictions`**
Create the folder first (`mkdir data\predictions`). On Linux the container user (10001) must be allowed to write it. TO VERIFY.

**`balanced: false` in the report, or exit code 1**
Events in must equal outcomes plus dropped. That is a bug, not a data problem: keep `stream_run.json` and open an issue.

**Grafana exits at once with `set GRAFANA_ADMIN_PASSWORD first`**
Set it in the same PowerShell window: `$env:GRAFANA_ADMIN_PASSWORD = "<choose one>"`, then `.\scripts\dev.ps1 upmon`.

**Grafana logs a `read-only file system` error (TO VERIFY)**
Grafana runs with a read-only root and a writable data volume. If it needs another writable path, add a `tmpfs:` entry for it under `grafana` in `docker-compose.yml` and report it, because the assistant could not run the container.

**Prometheus target `mmrisk-api` is DOWN**
Open http://127.0.0.1:9090/targets. The API must be healthy (`docker compose ps`); Prometheus reaches it as `api:8000` on the Compose network, not through the published port.

**The Grafana dashboard shows "No data"**
No traffic yet: rate and share panels are empty until requests arrive. Run `python -m mmrisk.api smoke` or the simulator. Check the time range (last hour).

**`python -m mmrisk.monitoring report` exits 2**
A mistake in how it was called: no predictions in the store (run `simulate` first), a bad `--window` (use 30m, 1h or 1d), or a `--baseline` that is not a file written by `baseline`.

**`WARN no unusual window` or a share WARN on a healthy store**
Fraud comes in clusters, so shares move more than chance alone predicts (see ADR-019). Compare with the previous run before acting; a FAIL is the signal that matters.

**`port is already allocated`**
Something uses port 8000. Change the first number in `127.0.0.1:8000:8000`, and the `--url` of `smoke`.

**`error: invalid cost` (exit code 2)**
A cost cannot be negative and the review effectiveness must be between 0 and 1. See `docs/cost-model.md`.
