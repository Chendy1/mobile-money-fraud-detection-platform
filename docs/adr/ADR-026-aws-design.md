# ADR-026: AWS design (design only)
Status: accepted (Phase 21). Document: [aws-design.md](../aws-design.md).

## Context
The local deployment (Phase 20) is the project's real deliverable. An AWS design shows how it would map to managed services, without creating anything: the project must stay near zero cost, and nothing is applied without approval.

## Decisions
- Design only. Phase 22 may write Terraform that is validated and planned, never applied by default.
- ECS on Fargate, one task per service, not EKS, EC2 or Lambda. The API keeps per-customer history in memory and replays it at start, which suits a long-running container and not a function.
- Files stay the data model: S3 for the lake and the registry. No RDS or warehouse; nothing needs one.
- No public endpoint by default. Access by SSM port forward to tasks in private subnets, so no load balancer, domain, certificate or internet-facing surface is needed. The self-signed certificate stays for the API's own TLS (it refuses outside loopback without TLS).
- Retraining stays outside AWS (host or CI); the registry is uploaded and promotion is a file plus a restart.
- Prometheus and Grafana are not moved; CloudWatch Logs and a few alarms only. Revisit if live metrics on AWS are wanted.
- Identity: execution and task roles with named resources, no long-lived keys, GitHub OIDC for CI with a plan-only role, tags with an expiry.
- Costs: only figures read from AWS pages, with source and date; arithmetic shown on those figures only; everything else is "not verified". Cost controls are design elements (resting state of zero tasks, no NAT by default, a budget alert, teardown).

## Gaps found by designing it (not solved)
Secrets are files locally but environment variables on ECS; the registry and lake are folders that must be fetched from S3 at start; promotion needs a service restart. Each needs a small code change later (a start-up script and an S3 client); the design says so instead of assuming it away.

## Consequences
The doc lists many TO VERIFY items because nothing was run on AWS. A test guards the document's structure, keeps dollar figures inside the cost section and fails if a `.tf` file appears before Phase 22. The choice of region, network option and whether to spend anything at all is the owner's (section 11).
