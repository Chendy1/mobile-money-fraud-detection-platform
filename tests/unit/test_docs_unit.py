"""Phase 24: the documentation keeps its promises (links, index, status, sourced numbers)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
MD_FILES = [ROOT / "README.md", *sorted(DOCS.glob("*.md")), *sorted((DOCS / "adr").glob("*.md"))]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, drop punctuation, spaces to hyphens."""
    h = re.sub(r"[`*_]", "", heading.strip().lower())
    h = re.sub(r"[^\w\s-]", "", h)
    return re.sub(r"\s", "-", h)


def anchors(path: Path) -> set[str]:
    in_code, out = False, set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            out.add(slug(line.lstrip("#")))
    return out


def relative_links(path: Path):
    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for target in LINK.findall(line):
            if not target.startswith(("http://", "https://", "mailto:", "computer://")):
                yield target


@pytest.mark.parametrize("path", MD_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_relative_link_and_anchor_resolves(path):
    for target in relative_links(path):
        file_part, _, anchor = target.partition("#")
        dest = path if not file_part else (path.parent / file_part).resolve()
        assert dest.exists(), f"{path.name}: broken link {target}"
        if anchor and dest.suffix == ".md":
            assert anchor in anchors(dest), f"{path.name}: no heading for #{anchor} in {dest.name}"


def test_the_index_links_every_document_and_every_adr():
    index = (DOCS / "index.md").read_text(encoding="utf-8")
    linked = {Path(t.partition("#")[0]).name for t in LINK.findall(index)}
    for f in [*DOCS.glob("*.md"), *(DOCS / "adr").glob("*.md")]:
        if f.name != "index.md":
            assert f.name in linked, f"docs/index.md does not link {f.relative_to(ROOT)}"


def test_the_readme_status_matches_the_roadmap():
    roadmap = (DOCS / "roadmap.md").read_text(encoding="utf-8")
    last = int(re.search(r"Phases 0 to (\d+) complete", roadmap).group(1))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"Phases 0 to {last} complete" in readme
    assert f"Phases 0 to {last} complete" in (DOCS / "PROJECT_STATE.md").read_text(
        encoding="utf-8"
    ) or f"{last} complete" in (DOCS / "PROJECT_STATE.md").read_text(encoding="utf-8")


def test_no_leftover_placeholders_in_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for stale in ("To be added", "Planned capabilities", "Phase 17 complete"):
        assert stale not in readme


def test_the_readme_results_table_matches_the_model_card():
    card = (DOCS / "model-card.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def rows(text, name_pattern):
        out = {}
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.startswith("|") and re.match(name_pattern, cells[0]):
                out[cells[0]] = cells
        return out

    names = r"(Hand-written rules|Logistic regression|Random forest|LightGBM)"
    card_rows, readme_rows = rows(card, names), rows(readme, names)
    assert len(readme_rows) == 4 == len(card_rows)
    for name, cells in readme_rows.items():
        assert cells[1] == card_rows[name][1], f"{name}: test PR-AUC differs from the model card"
        assert cells[2] == card_rows[name][-1], f"{name}: cost differs from the model card"


def test_the_verification_and_limitations_pages_exist_with_their_sections():
    ver = (DOCS / "verification-status.md").read_text(encoding="utf-8")
    for part in ("## CI", "## Developer machine", "## Sandbox", "## Not verified"):
        assert part in ver
    lim = (DOCS / "limitations.md").read_text(encoding="utf-8")
    for part in ("## What the project is not", "## Evaluation limits", "## Open items"):
        assert part in lim


def test_figures_quoted_in_the_readme_and_the_demo_guide_are_in_the_model_card():
    card = (DOCS / "model-card.md").read_text(encoding="utf-8")
    # (file, the text it quotes, the text the model card must contain for that figure)
    quoted = [
        ("README.md", "107 fraud-labelled", "107 fraud-labelled"),
        ("README.md", "24,224", "24,224"),
        ("README.md", "0.502", "0.502"),
        ("README.md", "0.552", "0.552"),
        ("README.md", "70,126", "70,126"),
        ("README.md", "46%", "46%"),
        ("docs/demo.md", "107 frauds", "107 in the test window"),
    ]
    for path, quote, in_card in quoted:
        assert quote in (ROOT / path).read_text(encoding="utf-8"), (
            f"{path} no longer quotes {quote}"
        )
        assert in_card in card, f"{quote} (quoted in {path}) is not in the model card"


def test_statements_that_later_phases_made_false_are_gone_from_the_threat_model():
    text = (DOCS / "threat-model.md").read_text(encoding="utf-8")
    for stale in (
        "Never validated, planned or applied",
        "Containers never run by the assistant",
        "| No request rate limit;",
    ):
        assert stale not in text, f"threat-model.md still says: {stale}"
