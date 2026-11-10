# Cloud costs and controls (Phase 23)

**An estimate, not a quote, and nothing has been bought.** Every rate below was read from an AWS page by a page-reading tool, on the date shown. For most rows the page's own rate table was not in the text, so the number is the one printed in a pricing *example* on the page (marked `page-example`). That is not confirmed as the current table rate. Every total is a **lower bound**: it leaves out everything listed under "Not priced". Recheck each row on its page before relying on any of it.

The numbers are produced by a small tool, `python -m mmrisk.cloudcost`, from [`infrastructure/cost/prices.json`](../infrastructure/cost/prices.json) (rates, sources, dates) and named assumptions about use. A test keeps this page's totals equal to the tool's output.

## 1. The finding that changed the Terraform
Phase 21 and 22 said the resting state, with no task running, bills no compute. That is true, but incomplete: the **interface VPC endpoints are billed by the hour whether or not anything uses them**. Five endpoints, one zone each, left on all month come to $36.50 at the example rate, which is more than the secrets and image storage together by a wide margin. So Phase 23 changed `infrastructure/terraform`:

- New variable `endpoints_enabled`, default `false`. The five interface endpoints exist only while it is `true`.
- Both ECS services refuse `desired_count > 0` unless `endpoints_enabled` is `true` (a precondition), because a task cannot pull its image or read its secrets without them.
- Order of use: turn the endpoints on, start the tasks, work, stop the tasks, turn the endpoints off ([cloud-deployment.md](cloud-deployment.md)).

## 2. Scenarios (lower bound, USD for one month)
| Scenario | What runs | Total |
|---|---|---|
| `resting` | Nothing. Endpoints off, services at zero tasks. Only the five secrets and the image storage exist. | $2.15 |
| `session` | `resting`, plus one 8-hour session with the endpoints and both tasks on | $3.21 |
| `always-on` | Everything running all 730 hours of the month (what the design avoids) | $93.73 |

| Line | `resting` | `session` | `always-on` | Price kind |
|---|---|---|---|---|
| Secrets Manager (5 secrets) | $2.00 | $2.00 | $2.00 | example rate |
| ECR image storage (1.5 GB) | $0.15 | $0.15 | $0.15 | example rate |
| Interface endpoints (5, one zone each) | | $0.40 | $36.50 | example rate |
| Fargate API task (1 vCPU, 2 GB) | | $0.39 | $36.04 | example rate |
| Fargate dashboard task (0.5 vCPU, 1 GB) | | $0.20 | $18.02 | example rate |
| Endpoint data processed (1.5 GB of image pulls) | | $0.01 | $0.01 | page rate |
| CloudWatch Logs ingestion (0.1 GB; 2 GB) | | $0.05 | $1.00 | example rate |
| **Total (lower bound)** | **$2.15** | **$3.21** | **$93.73** | |

**ASSUMED use** (not measured; replace with real values): an 8-hour session; 1.5 GB for both images (read the real sizes with `docker images`); 1.5 GB pulled per start; 0.1 GB of logs per session and 2 GB a month always-on; task sizes equal to the Terraform defaults; 5 endpoints (with ECS Exec on); 5 secrets; 730 hours as a month. Two tests check that the endpoint count, the secret count and the task sizes equal what the Terraform files define.

Run it yourself, with your own assumptions:
```powershell
.\.venv\Scripts\python -m mmrisk.cloudcost estimate                                  # all three scenarios
.\.venv\Scripts\python -m mmrisk.cloudcost estimate --scenario session --session-hours 3 --ecr-gb 0.9
.\.venv\Scripts\python -m mmrisk.cloudcost estimate --json
```

What the table shows: the idle cost of the design is a couple of dollars, a short session costs about a dollar more, and leaving it running is what costs real money, mostly the tasks and the endpoints in roughly equal measure. None of it applies free-tier credits.

## 3. Prices used
| Id | Amount | Per | Region on the page | Kind | Source | Read on |
|---|---|---|---|---|---|---|
| `fargate_vcpu_second` | USD 0.000011244 | vCPU-second | us-east-1 | example | [fargate/pricing](https://aws.amazon.com/fargate/pricing/) | 2026-11-06 |
| `fargate_gb_second` | USD 0.000001235 | GB-second | us-east-1 | example | [fargate/pricing](https://aws.amazon.com/fargate/pricing/) | 2026-11-06 |
| `interface_endpoint_hour` | USD 0.01 | endpoint-zone-hour | us-east-1 | example | [privatelink/pricing](https://aws.amazon.com/privatelink/pricing/) | 2026-11-09 |
| `interface_endpoint_gb` | USD 0.01 | GB processed | not stated | page rate | [privatelink/pricing](https://aws.amazon.com/privatelink/pricing/) | 2026-11-09 |
| `secret_month` | USD 0.40 | secret-month | not stated | example | [secrets-manager/pricing](https://aws.amazon.com/secrets-manager/pricing/) | 2026-11-09 |
| `ecr_gb_month` | USD 0.10 | GB-month | not stated | example | [ecr/pricing](https://aws.amazon.com/ecr/pricing) | 2026-11-09 |
| `cwlogs_ingest_gb` | USD 0.50 | GB | us-east-1 | example | [cloudwatch/pricing](https://aws.amazon.com/cloudwatch/pricing/) | 2026-11-09 |
| `budget_action_day` | USD 0.10 | action-enabled budget-day beyond two | not stated | page rate | [aws-budgets/pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing) | 2026-11-09 |
| `nat_gateway_hour` | USD 0.045 | hour (comparison only) | us-east-2 as printed | page rate | [vpc/pricing](https://aws.amazon.com/vpc/pricing/) | 2026-11-06 |
| `public_ipv4_hour` | USD 0.005 | hour (comparison only) | not stated | page rate | [vpc/pricing](https://aws.amazon.com/vpc/pricing/) | 2026-11-06 |

AWS Budgets: the page says notifications are free and only budgets with *actions* beyond the first two are charged. The Terraform budget has alerts and no actions.

## 4. Not priced (so every total is a lower bound)
- S3 storage and requests: the US East table could not be read.
- Data transfer to or from the internet and between zones.
- CloudWatch Logs storage after ingestion.
- ECS Exec and SSM Session Manager.
- The VPC, subnet, route table and security groups, and the S3 gateway endpoint (believed free; not verified).
- Secrets Manager API calls (the example price is $0.05 per 10,000 calls; the count is not estimated).
- Free Tier and credits: the Secrets Manager page says new customers from 2025-07-15 get up to $200 in credits. Eligibility and expiry are not verified and none is applied.
- Taxes, support plans, and any price change after the dates above.
- Another region: a Cape Town (af-south-1) or other region's prices were not read.

## 5. Controls, and what each does and does not do
| Control | Where | What it does | What it does not do |
|---|---|---|---|
| Nothing running by default | `desired_count = 0` | No Fargate charge | Does not stop the endpoints (next row) |
| Endpoints off by default | `endpoints_enabled = false`, precondition on the services | Removes the largest idle hourly charge | Nothing if you leave it on |
| No NAT gateway, no public IPv4, no load balancer, no database | Terraform tests forbid them | Removes their hourly charges (a NAT gateway has an hourly rate of USD 0.045 as printed for Ohio, plus a per-GB charge) | |
| Budget alert by email at 50, 80 and 100 percent actual and 100 percent forecast | `budgets.tf`, amount and address are yours | Tells you | **Does not stop spending** |
| Tags `Project`, `Owner`, `Expires` | provider default tags | Makes leftovers findable | Advisory only |
| Log retention (14 days, ASSUMED) | `logs.tf` | Bounds log storage | |
| Image lifecycle (keep 5), immutable tags | `ecr.tf` | Bounds image storage | |
| S3 lifecycle (old versions 30 days, incomplete uploads 7 days) | `s3.tf` | Bounds version storage | |
| `force_destroy = false` on buckets | `s3.tf` | A mistaken destroy cannot silently delete data | Makes teardown a deliberate manual step |
| Session discipline | [cloud-deployment.md](cloud-deployment.md) | The only thing that stops the always-on cost | Depends on you |

## 6. Habits that matter more than any setting
1. Decide the monthly limit first. Put a number you would not mind losing in `monthly_budget_usd`.
2. After every session, confirm in the console or CLI that no task is running and no interface endpoint exists (commands in the runbook). The budget alert is a smoke detector, not a sprinkler.
3. Look at the bill the day after a session. How soon billing data appears is TO VERIFY.
4. If you stop using it, destroy everything (runbook, "Teardown") instead of leaving the resting state: it is small, but it is not zero.
5. Do not apply this just to make the project look deployed. The portfolio claim that is true today is: *designed, written as Terraform, policy-tested, never applied*. Say that.

## 7. What was and was not verified
- Verified in the sandbox: the arithmetic and its agreement with the Terraform counts and with this page (tests), and that the Terraform still parses and passes its policy tests with the new gate.
- Not verified: the rates themselves (page examples, read through a page summariser, so a figure could be mis-copied), that the page examples are current table rates, every item under "Not priced", AWS Budgets behaviour, and anything about a real account.

Sources: [Fargate](https://aws.amazon.com/fargate/pricing/), [PrivateLink](https://aws.amazon.com/privatelink/pricing/), [Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), [ECR](https://aws.amazon.com/ecr/pricing), [CloudWatch](https://aws.amazon.com/cloudwatch/pricing/), [Budgets](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing), [VPC](https://aws.amazon.com/vpc/pricing/).
