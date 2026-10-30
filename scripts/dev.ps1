# Windows task runner (Makefile equivalent). Run from the repo root.
# Usage: .\scripts\dev.ps1 setup | lint | format | test | generate | ingest | quality | eda | features | train | advanced | explain | card | pipeline | compare | apicheck | serve | up | down | smoke | simulate | predictions | baseline | monitor | upmon | driftref | drift | regboot | regls | retrain-check | retrain | dashboard | dashcheck
param([Parameter(Mandatory = $true)][ValidateSet("setup", "lint", "format", "test", "generate", "ingest", "quality", "eda", "features", "train", "advanced", "explain", "card", "pipeline", "compare", "apicheck", "serve", "up", "down", "smoke", "simulate", "predictions", "baseline", "monitor", "upmon", "driftref", "drift", "regboot", "regls", "retrain-check", "retrain", "dashboard", "dashcheck")][string]$Task)

$ErrorActionPreference = "Stop"
$venvPython = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"

switch ($Task) {
    "setup" {
        python -m venv .venv
        & $venvPython -m pip install --upgrade pip
        & $venvPython -m pip install -e ".[dev,eda,ml,explain,tracking,api,dashboard]"
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
    "explain" { & $venvPython -m mmrisk.models explain }
    "card" { & $venvPython -m mmrisk.models card }
    "pipeline" { & $venvPython -m mmrisk.pipeline run }
    "compare" { & $venvPython -m mmrisk.pipeline compare }
    "apicheck" { & $venvPython -m mmrisk.api check }
    "serve" { & $venvPython -m mmrisk.api serve }
    "up" { docker compose up --build }
    "down" { docker compose --profile monitoring --profile simulate down }
    "smoke" { & $venvPython -m mmrisk.api smoke }
    "simulate" { & $venvPython -m mmrisk.streaming run --jitter-seconds 20 --duplicate-rate 0.01 --bad-rate 0.005 }
    "baseline" { & $venvPython -m mmrisk.monitoring baseline }
    "monitor" {
        $base = "data\monitoring\baseline.json"
        if (Test-Path $base) {
            & $venvPython -m mmrisk.monitoring report --baseline $base
        } else {
            & $venvPython -m mmrisk.monitoring report
        }
    }
    "driftref" { & $venvPython -m mmrisk.drift reference }
    "drift" { & $venvPython -m mmrisk.drift report }
    "regboot" { & $venvPython -m mmrisk.registry bootstrap }
    "regls" { & $venvPython -m mmrisk.registry list }
    "retrain-check" { & $venvPython -m mmrisk.retrain check }
    "retrain" { & $venvPython -m mmrisk.retrain run }
    "dashboard" { & $venvPython -m mmrisk.dashboard run }
    "dashcheck" { & $venvPython -m mmrisk.dashboard check }
    "upmon" { docker compose --profile monitoring up --build }
    "predictions" { & $venvPython -m mmrisk.streaming summary }
}
