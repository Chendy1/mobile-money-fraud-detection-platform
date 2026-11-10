# ADR-028: Cloud cost controls and an estimator with sourced prices
Status: accepted (Phase 23). Documents: [cloud-costs.md](../cloud-costs.md), [cloud-deployment.md](../cloud-deployment.md).

## Context
Phases 21 and 22 designed and wrote the AWS stack with "zero tasks" as the resting state and said that costs would be priced later. Pricing it showed that the resting state was not free: interface VPC endpoints are billed per endpoint per hour with nothing using them. The project rule is that no cost is stated unless it was read from a pricing page.

## Decisions
- **Gate the endpoints.** `endpoints_enabled` (default false) creates the five interface endpoints; both ECS services refuse `desired_count > 0` without it (a Terraform precondition). The lifecycle of a session is: endpoints on, tasks on, work, tasks off, endpoints off.
- **An estimator, not a spreadsheet of remembered numbers.** `python -m mmrisk.cloudcost` reads rates from `infrastructure/cost/prices.json`. Each row has an `aws.amazon.com` source, the date read, a region, a unit and a kind: `page-rate` or `page-example` (the number is printed in a pricing example because the rate table is not in the page text a reader gets). Estimates print the kind next to each line.
- **Lower bounds only.** Each result lists what is not priced (S3, data transfer, log storage, credits, taxes, other regions). Free-tier credits are never applied.
- **Assumptions are named inputs** with defaults labelled ASSUMED and command-line overrides; tests check that the counts and sizes equal what the Terraform defines, so the estimate cannot drift from the code unnoticed.
- **The document is tied to the tool by a test** (totals and every dollar figure must come from the prices file or the tool).
- **A runbook, not a button.** `cloud-deployment.md` is a session-based procedure with a decision gate, a stop rule and a teardown. Every step is TO VERIFY. The missing start-up script is specified, not written.
- **No automatic stop.** A budget only alerts. Anything that stops spending (budget actions, scheduled scale-down) adds IAM and moving parts and, for budget actions, a price after the first two; left out.

## Rejected
- Quoting remembered prices, or prices from third-party blog posts: the rule is the AWS page.
- Keeping the endpoints always on for convenience: the largest idle charge for no benefit.
- A NAT gateway to avoid endpoints: an hourly charge as well, and a per-GB one.
- Writing and shipping the start-up script untested: a script that handles secrets and cannot be run here is worse than an honest gap.

## Consequences
The estimate is only as good as the page examples it uses, and says so. The cheapest honest state is "not applied". If it is ever applied, the cost is dominated by whether someone remembers to end the session.
