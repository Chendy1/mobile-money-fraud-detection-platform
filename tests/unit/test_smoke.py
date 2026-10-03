"""Phase 1 smoke tests: the package imports and repo hygiene rules hold."""

import importlib
from pathlib import Path

import mmrisk

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_package_has_version():
    assert mmrisk.__version__ == "0.1.0"


def test_subpackages_importable():
    for name in (
        "ingestion",
        "validation",
        "features",
        "models",
        "inference",
        "monitoring",
        "utils",
    ):
        importlib.import_module(f"mmrisk.{name}")


def test_gitignore_blocks_secrets_and_generated_data():
    lines = {line.strip() for line in (REPO_ROOT / ".gitignore").read_text().splitlines()}
    for rule in (".env", "*.parquet", "*.tfstate", "mlruns/", "!.env.example"):
        assert rule in lines, f"missing .gitignore rule: {rule}"


def test_env_example_has_no_real_secrets():
    text = (REPO_ROOT / ".env.example").read_text()
    assert "change-me" in text
    assert "AKIA" not in text  # AWS access key id prefix


def test_license_is_apache_2():
    assert "Apache License" in (REPO_ROOT / "LICENSE").read_text()
