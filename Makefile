# Linux/macOS convenience targets. On Windows use: .\scripts\dev.ps1 <task>
.PHONY: setup lint format test generate ingest quality eda
setup:
	python -m venv .venv && .venv/bin/pip install -e ".[dev,eda]"
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
