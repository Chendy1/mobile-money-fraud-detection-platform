"""Phase 23: the cost estimate. Rates come only from the prices file; nothing is invented."""

import json
import re
from pathlib import Path

import pytest

from mmrisk.cloudcost import Assumptions, CostError, estimate, load_prices
from mmrisk.cloudcost.cli import main
from mmrisk.cloudcost.model import render

ROOT = Path(__file__).resolve().parents[2]
PRICES = ROOT / "infrastructure" / "cost" / "prices.json"

API_HOUR = (1 * 0.000011244 + 2 * 0.000001235) * 3600
DASH_HOUR = (0.5 * 0.000011244 + 1 * 0.000001235) * 3600


@pytest.fixture(scope="module")
def loaded():
    return load_prices(PRICES)


def est(loaded, scenario, **kw):
    prices, not_priced = loaded
    return estimate(prices, not_priced, scenario, Assumptions(**kw) if kw else None)


def by_item(e):
    return {line.item.split(" (")[0]: line for line in e.lines}


# --- the prices file -------------------------------------------------------------------------


def test_every_price_has_a_source_date_region_unit_kind_and_a_positive_amount(loaded):
    prices, _ = loaded
    assert len(prices) >= 8
    for pid, row in prices.items():
        assert row["source"].startswith("https://aws.amazon.com/"), pid
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["read_on"]), pid
        assert row["kind"] in ("page-rate", "page-example"), pid
        assert row["amount"] > 0 and row["unit"].startswith("USD"), pid
        assert row["region"] and row["description"], pid


def test_the_file_says_what_an_example_price_means_and_what_is_not_priced(loaded):
    text = PRICES.read_text(encoding="utf-8")
    assert "page-example" in json.loads(text)["_about"] and "Nothing here is a quote" in text
    _, not_priced = loaded
    joined = " ".join(not_priced)
    for word in ("S3", "Data transfer", "CloudWatch Logs storage", "Free Tier", "Taxes"):
        assert word in joined


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.pop("prices"), "needs 'prices'"),
        (lambda d: d["prices"]["secret_month"].pop("source"), "no 'source'"),
        (lambda d: d["prices"]["secret_month"].update(amount=0), "positive amount"),
        (lambda d: d["prices"]["secret_month"].update(amount="1"), "positive amount"),
        (lambda d: d["prices"]["secret_month"].update(kind="guess"), "kind must be"),
        (
            lambda d: d["prices"]["secret_month"].update(source="https://example.com/p"),
            "aws.amazon.com",
        ),
        (lambda d: d.pop("not_priced"), "needs 'prices'"),
    ],
)
def test_a_damaged_prices_file_is_refused(tmp_path, mutate, message):
    data = json.loads(PRICES.read_text(encoding="utf-8"))
    mutate(data)
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CostError, match=message):
        load_prices(bad)


def test_a_missing_or_unparseable_prices_file_is_refused(tmp_path):
    with pytest.raises(CostError, match="cannot read"):
        load_prices(tmp_path / "none.json")
    junk = tmp_path / "j.json"
    junk.write_text("{not json", encoding="utf-8")
    with pytest.raises(CostError, match="cannot read"):
        load_prices(junk)


# --- the arithmetic --------------------------------------------------------------------------


def test_resting_is_only_the_secrets_and_the_image_storage(loaded):
    e = est(loaded, "resting")
    assert [line.price_id for line in e.lines] == ["secret_month", "ecr_gb_month"]
    assert e.total == pytest.approx(5 * 0.40 + 1.5 * 0.10)


def test_a_session_adds_endpoints_tasks_pulls_and_logs_for_the_session_hours_only(loaded):
    e = est(loaded, "session")
    items = by_item(e)
    assert items["Interface endpoints"].cost == pytest.approx(5 * 8 * 0.01)
    assert items["Fargate: API task"].cost == pytest.approx(API_HOUR * 8)
    assert items["Fargate: Dashboard task"].cost == pytest.approx(DASH_HOUR * 8)
    assert items["Endpoint data processed"].cost == pytest.approx(1.5 * 0.01)
    assert items["CloudWatch Logs ingestion"].cost == pytest.approx(0.1 * 0.50)
    assert e.total == pytest.approx(2.15 + 0.40 + (API_HOUR + DASH_HOUR) * 8 + 0.015 + 0.05)


def test_always_on_runs_everything_for_730_hours(loaded):
    e = est(loaded, "always-on")
    items = by_item(e)
    assert items["Interface endpoints"].cost == pytest.approx(36.50)
    assert items["Fargate: API task"].cost == pytest.approx(API_HOUR * 730)
    assert items["Fargate: Dashboard task"].cost == pytest.approx(DASH_HOUR * 730)
    assert items["CloudWatch Logs ingestion"].cost == pytest.approx(2.0 * 0.50)
    assert e.total == pytest.approx(2.15 + 36.5 + (API_HOUR + DASH_HOUR) * 730 + 0.015 + 1.0)


def test_leaving_the_endpoints_on_is_the_largest_idle_charge_and_the_default_avoids_it(loaded):
    resting = est(loaded, "resting").total
    endpoints_idle = 5 * 730 * 0.01
    assert endpoints_idle > 10 * resting  # why endpoints_enabled defaults to false
    assert (
        est(loaded, "resting").total < est(loaded, "session").total < est(loaded, "always-on").total
    )


def test_session_hours_scale_the_compute_lines_linearly_and_zero_hours_cost_nothing(loaded):
    base = est(loaded, "session", session_hours=4.0)
    double = est(loaded, "session", session_hours=8.0)
    zero = est(loaded, "session", session_hours=0.0)
    for item in ("Interface endpoints", "Fargate: API task", "Fargate: Dashboard task"):
        assert by_item(double)[item].cost == pytest.approx(2 * by_item(base)[item].cost)
        assert by_item(zero)[item].cost == 0


def test_the_image_size_changes_only_the_storage_line(loaded):
    small = est(loaded, "resting", ecr_gb=0.5)
    big = est(loaded, "resting", ecr_gb=3.0)
    assert by_item(big)["ECR image storage"].cost == pytest.approx(0.30)
    assert by_item(small)["ECR image storage"].cost == pytest.approx(0.05)
    assert (
        by_item(big)["Secrets Manager secrets"].cost
        == by_item(small)["Secrets Manager secrets"].cost
    )


def test_the_task_sizes_come_from_the_assumptions(loaded):
    e = est(loaded, "session", api_vcpu=2.0, api_gb=4.0)
    assert by_item(e)["Fargate: API task"].cost == pytest.approx(2 * API_HOUR * 8)


def test_no_free_tier_or_credit_is_applied(loaded):
    # 5 GB of free log ingestion exists on the page, yet 2 GB of logs still cost money here
    assert by_item(est(loaded, "always-on"))["CloudWatch Logs ingestion"].cost > 0
    assert any("Free Tier" in x for x in est(loaded, "resting").not_priced)


def test_the_estimate_says_it_is_a_lower_bound_and_lists_what_is_not_priced(loaded):
    for scenario in ("resting", "session", "always-on"):
        e = est(loaded, scenario)
        text = render(e)
        assert "lower bound" in text and "Not priced:" in text and e.not_priced
        assert "[example rate]" in text


def test_the_json_form_marks_every_line_with_the_kind_of_price_used(loaded):
    d = est(loaded, "session").as_dict()
    assert d["total_lower_bound_usd"] == round(est(loaded, "session").total, 2)
    assert {line["kind"] for line in d["lines"]} <= {"page-rate", "page-example"}
    assert all(line["price_id"] for line in d["lines"])


@pytest.mark.parametrize(
    "bad", [{"session_hours": -1}, {"ecr_gb": -0.1}, {"pull_gb_per_start": -1}]
)
def test_negative_use_is_refused(loaded, bad):
    with pytest.raises(CostError, match="negative"):
        est(loaded, "session", **bad)


def test_an_unknown_scenario_or_a_missing_price_is_refused(loaded):
    prices, not_priced = loaded
    with pytest.raises(CostError, match="scenario must be"):
        estimate(prices, not_priced, "forever")
    thin = {k: v for k, v in prices.items() if k != "interface_endpoint_hour"}
    with pytest.raises(CostError, match="interface_endpoint_hour"):
        estimate(thin, not_priced, "session")


# --- the command -----------------------------------------------------------------------------


def test_the_command_prints_all_three_scenarios_by_default(capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert main(["estimate"]) == 0
    out = capsys.readouterr().out
    for name in ("resting", "session", "always-on"):
        assert f"Scenario: {name}" in out


def test_the_command_json_option_and_overrides(capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert main(["estimate", "--scenario", "session", "--json", "--session-hours", "2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1 and data[0]["scenario"] == "session"
    assert data[0]["assumptions"]["session_hours"] == 2.0


def test_the_command_fails_cleanly_without_a_prices_file(capsys, tmp_path):
    assert main(["estimate", "--prices", str(tmp_path / "none.json")]) == 2
    assert "error:" in capsys.readouterr().err


def test_the_command_rejects_an_unknown_scenario():
    with pytest.raises(SystemExit) as exc:
        main(["estimate", "--scenario", "forever"])
    assert exc.value.code == 2


# --- agreement with the Terraform and the documents -------------------------------------------


def test_the_assumed_counts_match_what_the_terraform_creates():
    network = (ROOT / "infrastructure" / "terraform" / "network.tf").read_text(encoding="utf-8")
    base = re.search(r"concat\(\s*\[([^\]]*)\]", network).group(1)
    exec_only = re.search(r'enable_execute_command \? \[("[^"]+")\]', network).group(1)
    assert len(re.findall(r'"[^"]+"', base)) + 1 == Assumptions().interface_endpoints
    assert exec_only == '"ssmmessages"'
    secrets = (ROOT / "infrastructure" / "terraform" / "secrets.tf").read_text(encoding="utf-8")
    names = re.search(r"secret_names = \[([^\]]*)\]", secrets).group(1)
    assert len(re.findall(r'"[^"]+"', names)) == Assumptions().secrets


def test_the_task_sizes_match_the_terraform_defaults():
    variables = (ROOT / "infrastructure" / "terraform" / "variables.tf").read_text(encoding="utf-8")

    def default(name):
        block = re.search(rf'variable "{name}" \{{(.*?)\n\}}', variables, re.S).group(1)
        return int(re.search(r"default\s*=\s*(\d+)", block).group(1))

    a = Assumptions()
    assert default("api_cpu") / 1024 == a.api_vcpu and default("api_memory") / 1024 == a.api_gb
    assert default("dashboard_cpu") / 1024 == a.dashboard_vcpu
    assert default("dashboard_memory") / 1024 == a.dashboard_gb


def test_the_cost_document_quotes_the_totals_the_tool_prints(loaded):
    doc = (ROOT / "docs" / "cloud-costs.md").read_text(encoding="utf-8")
    for scenario in ("resting", "session", "always-on"):
        assert f"${est(loaded, scenario).total:.2f}" in doc, scenario
    assert "lower bound" in doc and "example rate" in doc
    assert "python -m mmrisk.cloudcost estimate" in doc


def test_dollar_figures_in_the_cost_document_come_from_the_prices_or_the_tool(loaded):
    prices, _ = loaded
    doc = (ROOT / "docs" / "cloud-costs.md").read_text(encoding="utf-8")
    allowed = {f"${row['amount']:.2f}" for row in prices.values()}
    allowed |= {f"${row['amount']}" for row in prices.values()}
    allowed |= {f"${est(loaded, s).total:.2f}" for s in ("resting", "session", "always-on")}
    allowed |= {f"${line.cost:.2f}" for s in ("resting", "session", "always-on")
                for line in est(loaded, s).lines}  # fmt: skip
    allowed |= {"$0.05", "$0.01", "$200", "$0", "$20"}  # named in prose: calls, budget example
    found = set(re.findall(r"\$\d[\d,]*(?:\.\d+)?", doc))
    unexplained = {x for x in found if x not in allowed and x.rstrip("0") not in allowed}
    assert not unexplained, unexplained
