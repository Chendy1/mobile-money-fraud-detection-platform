# Linux/macOS convenience targets. On Windows use: .\scripts\dev.ps1 <task>
.PHONY: setup lint format test generate ingest quality eda features train advanced explain card pipeline compare apicheck serve up down smoke simulate predictions baseline monitor upmon driftref drift regboot regls retrain-check retrain dashboard dashcheck
setup:
	python -m venv .venv && .venv/bin/pip install -e ".[dev,eda,ml,explain,tracking,api,dashboard]"
lint:
	ruff check . && ruff format --check .
format:
	ruff check --fix . && ruff format .
test:
	pytest
generate:
	python -m mmrisk.generation
ingest:
	python -m mmrisk.ingestion run
quality:
	python -m mmrisk.validation check
eda:
	python -m mmrisk.eda
features:
	python -m mmrisk.features build
train:
	python -m mmrisk.models train
advanced:
	python -m mmrisk.models advanced
explain:
	python -m mmrisk.models explain
card:
	python -m mmrisk.models card
pipeline:
	python -m mmrisk.pipeline run
compare:
	python -m mmrisk.pipeline compare
apicheck:
	python -m mmrisk.api check
serve:
	python -m mmrisk.api serve
up:
	docker compose up --build
down:
	docker compose --profile monitoring --profile simulate down
smoke:
	python -m mmrisk.api smoke

simulate:
	python -m mmrisk.streaming run --jitter-seconds 20 --duplicate-rate 0.01 --bad-rate 0.005

predictions:
	python -m mmrisk.streaming summary

baseline:
	python -m mmrisk.monitoring baseline

monitor:
	python -m mmrisk.monitoring report $(if $(wildcard data/monitoring/baseline.json),--baseline data/monitoring/baseline.json)

driftref:
	python -m mmrisk.drift reference

drift:
	python -m mmrisk.drift report

upmon:
	docker compose --profile monitoring up --build

regboot:
	python -m mmrisk.registry bootstrap

regls:
	python -m mmrisk.registry list

retrain-check:
	python -m mmrisk.retrain check

retrain:
	python -m mmrisk.retrain run

dashboard:
	python -m mmrisk.dashboard run

dashcheck:
	python -m mmrisk.dashboard check
