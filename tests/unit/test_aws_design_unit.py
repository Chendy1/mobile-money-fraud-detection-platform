"""Phase 21 is a document; these tests keep it honest (structure, sourced costs, no Terraform)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "aws-design.md").read_text(encoding="utf-8")
ADR = (ROOT / "docs" / "adr" / "ADR-026-aws-design.md").read_text(encoding="utf-8")

SECTIONS = [
    "Goal and non-goals", "Mapping from the local stack", "Architecture", "Network",
    "Compute and secrets", "Data", "Retraining", "Observability", "Identity and access",
    "Costs", "Decisions for you", "Rejected",
]  # fmt: skip


def section(title):
    m = re.search(rf"^## \d+\. {re.escape(title)}.*?$(.*?)(?=^## |\Z)", DOC, re.S | re.M)
    assert m, f"missing section {title!r}"
    return m.group(1)


@pytest.mark.parametrize("title", SECTIONS)
def test_every_required_section_exists_and_is_not_empty(title):
    assert len(section(title).strip()) > 100


def test_the_document_says_it_is_a_design_and_nothing_was_created():
    head = DOC[:900].lower()
    assert "design only" in head and "nothing was created" in head and "no aws account" in head
    assert "`apply` needs your explicit approval" in head


def test_dollar_figures_appear_only_in_the_costs_section():
    outside = DOC.replace(section("Costs"), "")
    assert "$" not in outside
    assert "$" not in ADR


def test_every_sourced_price_row_has_a_link_and_unverified_items_have_no_number():
    costs = section("Costs")
    rows = [r for r in costs.splitlines() if r.startswith("| ") and "$" in r]
    assert rows
    for r in rows:
        assert "](https://aws.amazon.com/" in r, r
    unverified = re.search(r"\*\*Not priced in this document[^\n]*?\*\*(.*)", costs).group(1)
    assert "$" not in unverified and "Secrets Manager" in unverified


def test_the_arithmetic_in_the_document_matches_the_sourced_rates():
    vcpu_h, gb_h, hours = 0.000011244 * 3600, 0.000001235 * 3600, 730
    api = 1 * vcpu_h * hours + 2 * gb_h * hours
    dash = 0.5 * vcpu_h * hours + 1 * gb_h * hours
    assert f"${api:.2f}" in DOC and f"${dash:.2f}" in DOC
    assert f"${api + dash:.0f}" in DOC.replace(
        "about $54 a month", f"about ${api + dash:.0f} a month"
    )
    assert f"${0.045 * hours:.2f}" in DOC and f"${0.005 * hours:.2f}" in DOC
    assert f"${8 * (vcpu_h + 2 * gb_h):.2f}" in DOC


def test_unverified_claims_are_marked_and_the_gaps_are_listed():
    assert DOC.count("TO VERIFY") >= 8
    gaps = section("Compute and secrets")
    for word in ("files locally", "tmpfs", "S3", "restart", "TLS"):
        assert word in gaps


def test_the_decisions_that_belong_to_the_owner_are_listed():
    d = section("Decisions for you")
    for word in ("Region", "Network", "Public endpoint", "Secrets", "AWS spend"):
        assert word in d


def test_least_privilege_and_no_long_lived_keys_are_stated():
    iam = section("Identity and access")
    assert "no long-lived access keys" in iam.lower() and "OpenID Connect" in iam
    assert "No wildcard" in iam


def test_terraform_files_live_only_in_the_plan_only_folder_with_no_state():
    skip = {".git", ".venv", "node_modules", "mlruns", ".ruff_cache"}
    found = [
        p for p in ROOT.rglob("*")
        if (p.suffix in (".tf", ".tfvars", ".tfstate", ".tfplan") or p.name == ".terraform")
        and not (set(p.parts) & skip)
    ]  # fmt: skip
    folder = ROOT / "infrastructure" / "terraform"
    assert found and all(folder in p.parents for p in found), found
    assert not [p for p in found if p.suffix in (".tfvars", ".tfstate", ".tfplan")]


def test_the_doc_and_repository_hold_no_account_ids_or_keys():
    assert not re.search(r"\b\d{12}\b", DOC), "looks like an AWS account id"
    assert not re.search(r"AKIA[0-9A-Z]{16}", DOC + ADR)


@pytest.mark.skipif(shutil.which("mmdc") is None, reason="mermaid CLI not installed")
def test_every_diagram_parses(tmp_path):
    blocks = re.findall(r"```mermaid\n(.*?)```", DOC, re.S)
    assert blocks
    for i, b in enumerate(blocks):
        src, out = tmp_path / f"d{i}.mmd", tmp_path / f"d{i}.svg"
        src.write_text(b)
        done = subprocess.run(["mmdc", "-i", str(src), "-o", str(out)], capture_output=True,
                              text=True, timeout=180)  # fmt: skip
        assert done.returncode == 0 and out.exists(), done.stderr[-500:]
