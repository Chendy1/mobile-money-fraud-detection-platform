"""Phase 26: the resume text, the portfolio guide and the interview sheet keep to the evidence."""

import re
from pathlib import Path

import pytest

from mmrisk.features import FEATURES

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
RESUME = (DOCS / "resume.md").read_text(encoding="utf-8")
PORTFOLIO = (DOCS / "portfolio.md").read_text(encoding="utf-8")
INTERVIEW = (DOCS / "interview-prep.md").read_text(encoding="utf-8")


def between(text, start, end):
    i, j = text.index(start) + len(start), text.index(end)
    return text[i:j]


BULLETS = between(RESUME, "<!-- bullets:start -->", "<!-- bullets:end -->")
BULLET_LINES = [ln for ln in BULLETS.splitlines() if ln.startswith("- **B")]


def ledger_rows():
    section = RESUME[RESUME.index("## Claims ledger") : RESUME.index("## Banned wording")]
    rows = []
    for ln in section.splitlines():
        cells = [c.strip().strip("`") for c in ln.strip().strip("|").split("|")]
        if ln.startswith("|") and len(cells) == 4 and not cells[0].startswith(("Figure", "---")):
            rows.append(cells)
    return rows


def figures(text):
    """Numbers a reader would quote: two or more characters, so '46%' and '28' but not 'one'/'2'."""
    return {
        t.rstrip(".,")
        for t in re.findall(r"\d[\d,]*(?:\.\d+)?%?", text)
        if len(t.rstrip(".,")) >= 2
    }


# ---- the bullets -------------------------------------------------------------------------------


def test_there_are_twelve_numbered_bullets_in_order():
    ids = [re.match(r"- \*\*(B\d+)\*\*", ln).group(1) for ln in BULLET_LINES]
    assert ids == [f"B{i}" for i in range(1, 13)]


def test_every_bullet_has_an_evidence_row_and_nothing_extra():
    table = RESUME[RESUME.index("## Evidence for each bullet") : RESUME.index("## Claims ledger")]
    in_table = set(re.findall(r"^\| (B\d+) \|", table, flags=re.M))
    in_bullets = {re.match(r"- \*\*(B\d+)\*\*", ln).group(1) for ln in BULLET_LINES}
    assert in_table == in_bullets


def test_every_figure_in_the_bullets_is_in_the_ledger():
    ledger = {r[0] for r in ledger_rows()}
    for ln in BULLET_LINES:
        missing = figures(re.sub(r"^- \*\*B\d+\*\*", "", ln)) - ledger  # the label is not a claim
        assert not missing, f"{ln[:40]}... uses unledgered figures {missing}"


def test_every_ledger_figure_is_used_by_a_bullet():
    used = figures(re.sub(r"- \*\*B\d+\*\*", "", BULLETS))
    for fig, *_ in ledger_rows():
        assert fig in used, f"ledger figure {fig} is not used in any bullet"


def test_every_ledger_figure_is_found_in_its_source_file():
    rows = ledger_rows()
    assert len(rows) >= 10
    for fig, match, source, _ in rows:
        path = ROOT / source
        assert path.exists(), f"{fig}: source {source} is missing"
        assert match in path.read_text(encoding="utf-8"), f"{fig}: '{match}' not in {source}"


def test_the_computed_figures_match_the_code_and_the_repository():
    assert len(FEATURES) == 28 and "28 point-in-time-safe features" in RESUME
    assert (
        len(list((DOCS / "adr").glob("ADR-*.md"))) == 29
        and "29 architecture decision records" in RESUME
    )


def test_the_test_count_claim_stays_below_the_recorded_count():
    status = (DOCS / "verification-status.md").read_text(encoding="utf-8")
    recorded = int(
        re.search(r"Full test suite \| (\d{3,5}) passed", status).group(1)
    )  # sandbox row
    assert "more than 1,100 automated tests" in RESUME and 1100 < recorded


@pytest.mark.parametrize(
    "pattern",
    [
        r"deployed (to|on) AWS",
        r"in production",
        r"production[- ]grade",
        r"real[- ]time at scale",
        r"reduced (fraud|losses)",
        r"\bsaved\b",
        r"improved .{0,40} by \d",
        r"real (customers|transactions|users)",
        r"Safaricom",
        r"state[- ]of[- ]the[- ]art",
        r"outperform",
        r"best model",
    ],
)
def test_the_bullets_use_none_of_the_banned_wording(pattern):
    assert not re.search(pattern, BULLETS, flags=re.I), pattern


def test_the_bullets_carry_their_qualifiers():
    by_id = {re.match(r"- \*\*(B\d+)\*\*", ln).group(1): ln for ln in BULLET_LINES}
    assert "synthetic" in by_id["B1"]
    assert "assumed" in by_id["B4"] and "46%" in by_id["B4"]
    assert "never applied" in by_id["B12"] and "plan-only" in by_id["B12"]
    assert "on my machine" in by_id["B7"]
    assert "did not beat" in by_id["B2"]


def test_the_banned_wording_section_lists_what_the_bullets_must_avoid():
    banned = RESUME[RESUME.index("## Banned wording") : RESUME.index("## LinkedIn")]
    for needle in ("Deployed to AWS", "Reduced fraud losses", "Safaricom", "Tested on Windows"):
        assert needle in banned


def test_the_linkedin_text_states_the_limits():
    text = RESUME[RESUME.index("## LinkedIn") :]
    assert (
        "synthetic" in text and "never applied" in text and "Not affiliated with Safaricom" in text
    )


# ---- the portfolio guide and the interview sheet ----------------------------------------------


def test_the_portfolio_guide_says_what_was_not_done_and_how_it_was_built():
    section = PORTFOLIO[
        PORTFOLIO.index("## How it was built") : PORTFOLIO.index("## What to say if asked")
    ]
    assert "AI coding assistant (Claude)" in section and "wrote most of the code" in section
    assert "never planned or applied" in PORTFOLIO
    assert "have **not** been run" in PORTFOLIO  # Prometheus and Grafana


def test_the_interview_sheet_covers_how_it_was_built_and_the_wrong_guess():
    assert "how to answer" in INTERVIEW.lower() and "AI coding assistant" in INTERVIEW
    assert "wrong guess" in INTERVIEW


def test_interview_numbers_match_the_model_card_and_other_sources():
    card = (DOCS / "model-card.md").read_text(encoding="utf-8")
    for fig in ("0.161", "0.515", "0.552", "0.502", "70,126", "57,125", "37,935", "24,224"):
        assert fig in INTERVIEW and fig in card, fig
    sourced = [
        ("$2.15", "cloud-costs.md"),
        ("$3.21", "cloud-costs.md"),
        ("$93.73", "cloud-costs.md"),
    ]
    sourced += [("12 of 12", "deployment.md"), ("10.7%", "eda-report.md")]
    sourced += [("5,000 customers", "eda-report.md")]
    for fig, src in sourced:
        assert fig in INTERVIEW and fig in (DOCS / src).read_text(encoding="utf-8"), fig


def test_the_interview_retraining_answer_matches_the_adr():
    adr = (DOCS / "adr" / "ADR-021-gated-retraining.md").read_text(encoding="utf-8")
    for fact in ("90 days", "200 fraud labels", "unknown"):
        assert fact in adr
        assert fact.split()[0] in INTERVIEW


def test_every_adr_cited_by_the_new_pages_exists():
    for text in (RESUME, PORTFOLIO, INTERVIEW):
        for n in set(re.findall(r"ADR-(\d{3})", text)):
            assert list((DOCS / "adr").glob(f"ADR-{n}-*.md")), f"ADR-{n} is cited but missing"
