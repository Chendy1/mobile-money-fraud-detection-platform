# Terraform (Phase 22): plan-only

The design in [aws-design.md](aws-design.md) written as Terraform in `infrastructure/terraform`. **It has never been validated, planned or applied.** The sandbox that wrote it had no provider registry and no AWS account; the tests parse the files and check the design's rules, nothing more. Decision record: [ADR-027](adr/ADR-027-terraform-plan-only.md).

**Rules of this phase.** `terraform apply` is not part of the project's path and needs your explicit decision. No credentials, account ids, keys, secret values, state or real variable files are in the repository (the tests check). No billable resource is created by anything in the repository itself.

## What the files describe (counted from the files, not from a plan)
52 resources with the defaults (57 when `endpoints_enabled` is true, which adds the five interface endpoints): one VPC with one private subnet and no internet gateway, no NAT gateway and no public IPs; two security groups (tasks: no inbound; endpoints: HTTPS from the tasks only); an S3 gateway endpoint and, only while `endpoints_enabled = true`, five interface endpoints (ECR API, ECR Docker, Secrets Manager, CloudWatch Logs, SSM messages), because they bill by the hour even with nothing running ([cloud-costs.md](cloud-costs.md)); three private S3 buckets (`lake`, `registry`, `backups`) with Block Public Access, encryption (S3-managed), TLS-only policies, versioning on two, lifecycle rules; two ECR repositories (immutable tags, scan on push, keep 5); five empty Secrets Manager secrets; two log groups with retention; two IAM roles with named-resource policies; an ECS cluster, two task definitions, two services at **zero tasks**; one budget with email alerts.

| File | Content |
|---|---|
| `versions.tf` | Terraform and provider versions, default tags, caller identity |
| `variables.tf` | Inputs with validation; `owner`, `expires`, `monthly_budget_usd`, `budget_alert_email` have **no default** |
| `network.tf`, `s3.tf`, `ecr.tf`, `secrets.tf`, `logs.tf`, `iam.tf`, `ecs.tf`, `budgets.tf`, `outputs.tf` | One area each |
| `terraform.tfvars.example` | Placeholders only |

## Run it yourself (nothing is created)
Install the Terraform CLI (Windows: `winget install Hashicorp.Terraform`, TO VERIFY) and give your shell AWS credentials for a **read-only** purpose (an SSO or named profile; never put keys in these files). `plan` reads from AWS and changes nothing.

```powershell
cd C:\Users\geek\projects\mobile-money-fraud-detection-platform
.\scripts\dev.ps1 tf-validate                       # fmt, init without a backend, validate; no AWS call
cd infrastructure\terraform
Copy-Item terraform.tfvars.example terraform.tfvars  # git-ignored; edit owner, expires, budget, email
terraform plan                                       # needs your AWS credentials; read-only
```
Expected from `tf-validate`: `Success! The configuration is valid.` Expected from `plan`: `Plan: 52 to add, 0 to change, 0 to destroy.` (my count; the real number is TO VERIFY) and no errors. **Do not run `terraform apply`.** If `plan` shows an error, send me the text: I could not run the provider here, so some argument names may be wrong for the provider version.

After the first `terraform init`, a `.terraform.lock.hcl` file appears: commit it (it pins the provider).

The step-by-step runbook for an eventual apply, with costs and teardown, is [cloud-deployment.md](cloud-deployment.md).

## Before anyone ever applies (checklist, not a recommendation to do it)
1. [cloud-costs.md](cloud-costs.md) read and a budget amount chosen. Its rates are page examples, not quotes, and S3 and data transfer are not priced.
2. The start-up script `/app/aws-entrypoint.sh` built into both images (writes the secrets to files, copies the registry and lake from S3). Until it exists a started task fails; that is why services rest at 0.
3. Images pushed with an immutable version tag; secrets' values put in by hand; registry and lake uploaded.
4. A teardown plan: `destroy` stops on non-empty buckets (`force_destroy = false`), secrets stay recoverable for the recovery window, ECR repositories keep images. That is deliberate.

## Known limits and TO VERIFY
- `validate` and `plan` never ran. Argument names for provider `~> 5.0` (security group rules, ECR `force_delete`, budgets notifications, S3 versioning `Disabled` on a new bucket) are from memory.
- Fargate: sizes for `api_cpu` and memory, dropping all capabilities, volumes standing in for `tmpfs`, and reaching a private task by SSM port forward are TO VERIFY (ADR-026).
- Interface endpoints in a single zone: some services may not offer every zone (TO VERIFY).
- Only one zone and one task of each service: no failover (stated non-goal).
- No GitHub OpenID Connect role yet: CI only runs `fmt` and `validate`; `plan` is run by you.
- Local state is the only option set up. State holds resource ids; it is git-ignored. Remote state would need a bucket and a lock, which this phase leaves out on purpose.
- `terraform fmt` was checked with OpenTofu 1.10 in the sandbox; the Terraform CLI's formatting may differ in small ways (CI runs the Terraform CLI, first result TO VERIFY).
