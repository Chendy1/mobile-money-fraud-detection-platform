# Linux/macOS convenience targets. On Windows use: .\scripts\dev.ps1 <task>
.PHONY: setup lint format test generate ingest quality eda features train
setup:
	python -m venv .venv && .venv/bin/pip install -e ".[dev,eda,ml]"
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
