# Windows task runner (Makefile equivalent). Run from the repo root.
# Usage: .\scripts\dev.ps1 setup | lint | format | test | generate | ingest | quality | eda | features | train
param([Parameter(Mandatory = $true)][ValidateSet("setup", "lint", "format", "test", "generate", "ingest", "quality", "eda", "features", "train")][string]$Task)

$ErrorActionPreference = "Stop"
$venvPython = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"

switch ($Task) {
    "setup" {
        python -m venv .venv
        & $venvPython -m pip install --upgrade pip
        & $venvPython -m pip install -e ".[dev,eda,ml]"
    }
    "lint"   { & $venvPython -m ruff check .; & $venvPython -m ruff format --check . }
    "format" { & $venvPython -m ruff check --fix .; & $venvPython -m ruff format . }
    "test"   { & $venvPython -m pytest }
    "generate" { & $venvPython -m mmrisk.generation }
    "ingest" { & $venvPython -m mmrisk.ingestion run }
    "quality" { & $venvPython -m mmrisk.validation check }
    "eda" { & $venvPython -m mmrisk.eda }
    "features" { & $venvPython -m mmrisk.features build }
    "train" { & $venvPython -m mmrisk.models train }
    "advanced" { & $venvPython -m mmrisk.models advanced }
}
