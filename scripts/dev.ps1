# Windows task runner (Makefile equivalent). Run from the repo root.
# Usage: .\scripts\dev.ps1 setup | lint | format | test | generate | ingest | quality | eda | features | train | advanced | explain | card | pipeline | compare | apicheck | serve | up | down | smoke | simulate | predictions | baseline | monitor | upmon | driftref | drift | regboot | regls | retrain-check | retrain | dashboard | dashcheck | secscan | audit | loadtest | inproc | deploy-init | preflight | up-prod | down-prod | verify | backup | tf-validate | cost
param([Parameter(Mandatory = $true)][ValidateSet("setup", "lint", "format", "test", "generate", "ingest", "quality", "eda", "features", "train", "advanced", "explain", "card", "pipeline", "compare", "apicheck", "serve", "up", "down", "smoke", "simulate", "predictions", "baseline", "monitor", "upmon", "driftref", "drift", "regboot", "regls", "retrain-check", "retrain", "dashboard", "dashcheck", "secscan", "audit", "loadtest", "inproc", "deploy-init", "preflight", "up-prod", "down-prod", "verify", "backup", "tf-validate", "cost", "demo", "demo-full", "demo-check", "demo-clean")][string]$Task)

$ErrorActionPreference = "Stop"
$venvPython = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"

switch ($Task) {
    "setup" {
        python -m venv .venv
        & $venvPython -m pip install --upgrade pip
        & $venvPython -m pip install -e ".[dev,eda,ml,explain,tracking,api,dashboard,deploy]"
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
    "loadtest" { & $venvPython -m mmrisk.loadtest bench --profile step --rates 10,25,50,75,100,125,150 --step-seconds 8 --last-days 14 }
    "inproc" { & $venvPython -m mmrisk.loadtest inproc }
    "cost" { & $venvPython -m mmrisk.cloudcost estimate }
    # guided demo (Enter between steps): demo = the short track, demo-full = every step
    "demo" { & $venvPython -m mmrisk.demo run --track short }
    "demo-full" { & $venvPython -m mmrisk.demo run --track standard }
    "demo-check" { & $venvPython -m mmrisk.demo check }
    "demo-clean" { & $venvPython -m mmrisk.demo clean }
    "tf-validate" {
        # needs the terraform CLI; makes no AWS call and needs no credentials
        terraform -chdir=infrastructure/terraform fmt -check -diff
        terraform -chdir=infrastructure/terraform init -backend=false -input=false
        terraform -chdir=infrastructure/terraform validate
    }
    "deploy-init" { & $venvPython -m mmrisk.deploy init }
    "preflight" { & $venvPython -m mmrisk.deploy preflight }
    "up-prod" { docker compose -f docker-compose.prod.yml up --build -d }
    "down-prod" { docker compose -f docker-compose.prod.yml --profile monitoring down }
    "verify" { & $venvPython -m mmrisk.deploy verify }
    "backup" {
        New-Item -ItemType Directory -Force backups | Out-Null
        & $venvPython -m mmrisk.deploy backup --out ("backups\mmrisk-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".tgz")
    }
    "secscan" { & $venvPython -m mmrisk.security scan }
    "audit" { & $venvPython -m pip install pip-audit; & $venvPython -m pip_audit --skip-editable }
    "upmon" { docker compose --profile monitoring up --build }
    "predictions" { & $venvPython -m mmrisk.streaming summary }
}
