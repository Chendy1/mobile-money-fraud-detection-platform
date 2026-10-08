# Linux/macOS convenience targets. On Windows use: .\scripts\dev.ps1 <task>
.PHONY: setup lint format test generate ingest
setup:
	python -m venv .venv && .venv/bin/pip install -e ".[dev]"
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
