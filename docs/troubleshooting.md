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
