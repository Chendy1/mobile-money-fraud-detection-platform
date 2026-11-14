"""Phase 28: the release text and commands stay true to the repository."""

import re
from pathlib import Path

import mmrisk
from mmrisk.features import FEATURES
from mmrisk.scorecard.model import evaluate_all
from mmrisk.scorecard.render import summary_counts

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
VERSION = mmrisk.__version__
NOTES = (DOCS / f"release-notes-v{VERSION}.md").read_text(encoding="utf-8")
RELEASING = (DOCS / "releasing.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_version_is_the_same_in_pyproject_changelog_notes_and_commands():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(rf'^version = "{re.escape(VERSION)}"', pyproject, flags=re.M)
    headings = re.findall(r"^## \[([^\]]+)\]", CHANGELOG, flags=re.M)
    assert headings[0] == "Unreleased" and headings[1] == VERSION
    assert NOTES.startswith(f"# v{VERSION}:")
    for cmd in (
        f"git tag -a v{VERSION}",
        f"git push origin v{VERSION}",
        f"gh release create v{VERSION}",
    ):
        assert cmd in RELEASING
    assert f"--notes-file docs/release-notes-v{VERSION}.md" in RELEASING
    assert "--verify-tag" in RELEASING


def test_the_unreleased_section_is_empty_after_the_release_heading():
    block = CHANGELOG.split("## [Unreleased]")[1].split(f"## [{VERSION}]")[0]
    assert block.strip() == ""


def test_notes_scorecard_counts_match_the_computed_levels():
    _, res = evaluate_all(DOCS / "scorecard.json", ROOT)
    # The notes describe the state at tagging time, after the CI run for this phase has been
    # recorded (releasing.md says to do that first). Until then, an area held back only because
    # CI has not yet covered its last change counts as E3.
    pending = sum(
        1 for r in res if r.level == 2 and "after the latest green CI run" in " ".join(r.notes)
    )
    c = summary_counts(res)
    c[3] += pending
    c[2] -= pending
    assert f"places {len(res)} areas" in NOTES
    assert f"{c[4]} are at E4" in NOTES and f"{c[3]} at E3" in NOTES
    assert c[5] == 0 and "none at E5" in NOTES


def test_notes_figures_match_the_repository():
    assert f"{len(FEATURES)} point-in-time-safe features" in NOTES
    adrs = len(list((DOCS / "adr").glob("ADR-*.md")))
    assert f"{adrs} architecture decision records" in NOTES
    verification = (DOCS / "verification-status.md").read_text(encoding="utf-8")
    recorded = int(
        re.search(r"Full test suite \| ([\d,]+) passed", verification).group(1).replace(",", "")
    )
    floor = int(re.search(r"more than ([\d,]+) automated tests", NOTES).group(1).replace(",", ""))
    assert floor < recorded
    assert "107 fraud-labelled" in NOTES and "107 fraud-labelled" in (
        DOCS / "model-card.md"
    ).read_text(encoding="utf-8") + (DOCS / "limitations.md").read_text(encoding="utf-8")
    assert "46%" in NOTES and "assumed costs" in NOTES


def test_notes_carry_the_disclosures():
    low = NOTES.lower()
    for need in (
        "synthetic data",
        "not affiliated with safaricom",
        "nothing was deployed to aws",
        "never planned or applied",
        "ai coding assistant",
        "not a security audit",
        "lightgbm did not beat the random forest",
        "apache 2.0",
    ):
        assert need in low, need


def test_notes_and_releasing_avoid_banned_wording():
    for text in (NOTES, RELEASING):
        low = text.lower()
        for bad in (
            "production-grade",
            "state-of-the-art",
            "outperforms",
            "deployed to aws",
            "real customers",
            "reduced fraud losses",
            "in production",
            "co-authored-by",
        ):
            if bad == "deployed to aws" and "nothing was deployed to aws" in low:
                continue
            assert bad not in low, bad


def test_releasing_says_what_is_not_attached_and_how_to_undo():
    for need in (
        "No data, models or secrets",
        "No Docker images",
        "gh release delete",
        "git push origin --delete",
        "TO VERIFY",
    ):
        assert need in RELEASING
    assert "dev.ps1 scorecard-check" in RELEASING and "dev.ps1 secscan" in RELEASING


def test_the_dev_tasks_named_in_releasing_exist():
    ps = (ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    for task in re.findall(r"dev\.ps1 ([a-z-]+)", RELEASING):
        assert f'"{task}"' in ps, task


def test_gitignore_keeps_secrets_and_generated_data_out_of_the_release():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("secrets/", "api_keys.json", "*.pem", "*.key", "*.parquet", ".env"):
        assert pattern in ignore
