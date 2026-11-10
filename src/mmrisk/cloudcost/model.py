"""Scenarios and arithmetic. Rates come only from the prices file."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

HOURS_PER_MONTH = 730  # ASSUMED convention for "one month"
KINDS = ("page-rate", "page-example")
SCENARIOS = ("resting", "session", "always-on")


class CostError(Exception):
    """A prices file or an input that cannot be used."""


@dataclass(frozen=True)
class Assumptions:
    """What the estimate assumes about use. All ASSUMED: replace with measured values."""

    session_hours: float = 8.0  # one working session with the tasks running
    ecr_gb: float = 1.5  # total size of both images (replace with `docker images`)
    pull_gb_per_start: float = 1.5  # image bytes pulled through the interface endpoints
    log_gb_month_always_on: float = 2.0
    log_gb_session: float = 0.1
    api_vcpu: float = 1.0
    api_gb: float = 2.0
    dashboard_vcpu: float = 0.5
    dashboard_gb: float = 1.0
    interface_endpoints: int = 5  # network.tf with ECS Exec on
    secrets: int = 5  # secrets.tf
    hours_per_month: int = HOURS_PER_MONTH


@dataclass(frozen=True)
class Line:
    item: str
    quantity: float
    unit: str
    price_id: str
    cost: float
    kind: str  # kind of the price used


@dataclass
class Estimate:
    scenario: str
    lines: list[Line] = field(default_factory=list)
    not_priced: list[str] = field(default_factory=list)
    assumptions: Assumptions = field(default_factory=Assumptions)

    @property
    def total(self) -> float:
        return sum(line.cost for line in self.lines)

    def as_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "total_lower_bound_usd": round(self.total, 2),
            "lines": [{**line.__dict__, "cost": round(line.cost, 2)} for line in self.lines],
            "not_priced": self.not_priced,
            "assumptions": self.assumptions.__dict__,
        }


def load_prices(path: Path | str) -> tuple[dict, list[str]]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CostError(f"cannot read prices file {p}: {exc}") from exc
    prices, not_priced = data.get("prices"), data.get("not_priced")
    if not isinstance(prices, dict) or not prices or not isinstance(not_priced, list):
        raise CostError("prices file needs 'prices' and 'not_priced'")
    for pid, row in prices.items():
        for key in ("amount", "unit", "region", "source", "read_on", "kind", "description"):
            if key not in row:
                raise CostError(f"price {pid!r} has no {key!r}")
        if not isinstance(row["amount"], (int, float)) or row["amount"] <= 0:
            raise CostError(f"price {pid!r} needs a positive amount")
        if row["kind"] not in KINDS:
            raise CostError(f"price {pid!r}: kind must be one of {KINDS}")
        if not str(row["source"]).startswith("https://aws.amazon.com/"):
            raise CostError(f"price {pid!r}: the source must be an aws.amazon.com page")
    return prices, [str(x) for x in not_priced]


def _rate(prices: dict, pid: str) -> tuple[float, str]:
    if pid not in prices:
        raise CostError(f"no price {pid!r} in the prices file")
    return float(prices[pid]["amount"]), prices[pid]["kind"]


def _fargate_cost(prices: dict, vcpu: float, gb: float, hours: float) -> tuple[float, str]:
    v, kv = _rate(prices, "fargate_vcpu_second")
    m, km = _rate(prices, "fargate_gb_second")
    kind = kv if kv == km else "page-example"
    return (vcpu * v + gb * m) * 3600 * hours, kind


def estimate(
    prices: dict, not_priced: list[str], scenario: str, a: Assumptions | None = None
) -> Estimate:
    """Lower-bound monthly cost for a scenario.

    resting    endpoints off, services at 0 tasks: only what always exists
    session    resting plus one session of `session_hours` with endpoints and both tasks on
    always-on  everything running all month (what the design avoids)
    """
    a = a or Assumptions()
    if scenario not in SCENARIOS:
        raise CostError(f"scenario must be one of {SCENARIOS}")
    if min(a.session_hours, a.ecr_gb, a.pull_gb_per_start, a.log_gb_session) < 0:
        raise CostError("assumptions cannot be negative")
    est = Estimate(scenario, not_priced=list(not_priced), assumptions=a)
    add = est.lines.append

    rate, kind = _rate(prices, "secret_month")
    add(Line("Secrets Manager secrets", a.secrets, "secret-months", "secret_month",
             a.secrets * rate, kind))  # fmt: skip
    rate, kind = _rate(prices, "ecr_gb_month")
    add(Line("ECR image storage", a.ecr_gb, "GB-months", "ecr_gb_month", a.ecr_gb * rate, kind))

    if scenario == "resting":
        return est

    hours = a.session_hours if scenario == "session" else float(a.hours_per_month)
    rate, kind = _rate(prices, "interface_endpoint_hour")
    add(Line("Interface endpoints (one zone each)", a.interface_endpoints * hours,
             "endpoint-hours", "interface_endpoint_hour",
             a.interface_endpoints * hours * rate, kind))  # fmt: skip
    for name, vcpu, gb in (("API task", a.api_vcpu, a.api_gb),
                           ("Dashboard task", a.dashboard_vcpu, a.dashboard_gb)):  # fmt: skip
        cost, kind = _fargate_cost(prices, vcpu, gb, hours)
        add(Line(f"Fargate: {name} ({vcpu:g} vCPU, {gb:g} GB)", hours, "task-hours",
                 "fargate_vcpu_second+fargate_gb_second", cost, kind))  # fmt: skip
    rate, kind = _rate(prices, "interface_endpoint_gb")
    add(Line("Endpoint data processed (image pulls)", a.pull_gb_per_start, "GB",
             "interface_endpoint_gb", a.pull_gb_per_start * rate, kind))  # fmt: skip
    logs = a.log_gb_session if scenario == "session" else a.log_gb_month_always_on
    rate, kind = _rate(prices, "cwlogs_ingest_gb")
    add(Line("CloudWatch Logs ingestion (free tier not applied)", logs, "GB",
             "cwlogs_ingest_gb", logs * rate, kind))  # fmt: skip
    return est


def render(est: Estimate) -> str:
    head = "lower bound, USD for one month; excludes what is not priced"
    out = [f"Scenario: {est.scenario}  ({head})"]
    for line in est.lines:
        flag = "  [example rate]" if line.kind == "page-example" else ""
        out.append(
            f"  {line.item:<52} {line.quantity:>9.2f} {line.unit:<15} ${line.cost:>8.2f}{flag}"
        )
    out.append(f"  {'TOTAL (lower bound)':<52} {'':>9} {'':<15} ${est.total:>8.2f}")
    out.append(
        "  ASSUMED use: " + ", ".join(f"{k}={v}" for k, v in est.assumptions.__dict__.items())
    )
    out.append("  Not priced:")
    out.extend(f"    - {x}" for x in est.not_priced)
    return "\n".join(out)
