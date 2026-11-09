# ADR-027: Terraform, plan-only
Status: accepted (Phase 22). Runbook: [terraform.md](../terraform.md). Builds on [ADR-026](ADR-026-aws-design.md).

## Context
Phase 21 designed the AWS deployment. Writing it as code makes the design reviewable and checkable, but the project must stay near zero cost, and the sandbox that wrote it could neither reach the provider registry nor an AWS account.

## Decisions
- Terraform with the AWS provider `~> 5.0`; a single root module with one file per area; no backend block (local state, ignored), no modules from the registry.
- Plan-only: CI runs `fmt -check`, `init -backend=false` and `validate`, which need no credentials and make no AWS call. `plan` is run by the owner with read-only credentials. `apply` is outside the project's path.
- The design's choices are the code's defaults: one zone, private subnet, no gateway or NAT, endpoints, S3-managed encryption (a customer key's price is unverified), services resting at zero tasks.
- Safety is in the inputs: owner, expiry, budget amount and alert email have no default; image tags cannot be `latest`; `desired_count` is 0 or 1.
- Secrets are empty containers. No secret version resource exists, so values never enter Terraform or its state.
- IAM names every resource. The only `"*"` resources are the actions AWS defines without one (`ecr:GetAuthorizationToken`, the SSM messages channel actions); a test fails if another appears or an exception goes unused.
- No GitHub OpenID Connect role yet: a plan-only CI role needs an account and a trust setup that cannot be checked here.
- The start-up gap from ADR-026 is written into the task definitions (an `entryPoint` that does not exist yet), so a started task fails loudly rather than running without its secrets.

## Tests (19 unit tests, parse-based) and what they cannot do
They parse the HCL and check: every reference resolves and every variable is used; no internet gateway, NAT, load balancer, database or public address; the only inbound rule is endpoints from tasks; buckets private, encrypted, TLS-only, never force-destroyed; IAM without wildcard actions and with exactly the two exceptions; no users, keys or secret values; tasks non-root, read-only, no capabilities; zero tasks by default; state and variable files ignored and absent; formatting when a binary exists. 23 mutations were tried against them and all were caught. They cannot show the provider accepts the arguments or that the plan is what is wanted: that is `validate` and `plan`, TO VERIFY.

## Rejected
- Terraform modules from the public registry: more code to trust for a small stack.
- Remote state: a bucket and a lock table to create before anything else, for one person.
- A NAT gateway or public subnets by default (ADR-026).
- `apply` in CI: no credentials in CI, no account decision.
