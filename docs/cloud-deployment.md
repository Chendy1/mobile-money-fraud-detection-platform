# Cloud deployment runbook (Phase 23)

**Nothing in this runbook has been run.** No AWS account was used to write it; every command and every AWS behaviour below is TO VERIFY. It exists so that if you ever decide to apply the Phase 22 Terraform, you do it in a controlled way with a known cost and a way out. Reading it costs nothing; following it spends money. Costs: [cloud-costs.md](cloud-costs.md). Design: [aws-design.md](aws-design.md). Terraform: [terraform.md](terraform.md).

**Decision gate.** Do not start unless you have decided (1) that you want any AWS spend at all, (2) a monthly limit you accept losing, (3) the region (the Terraform assumes `us-east-1`; Cape Town was not priced). The honest alternative is to stop at "designed, written as Terraform, policy-tested, never applied", which is already a complete portfolio statement.

**Three things that are not done yet** and must exist before a task can run:
1. The start-up script `/app/aws-entrypoint.sh` in both images (specification in section 7).
2. A decision on how you reach the service (section 5 assumes an SSM port forward to a private task; TO VERIFY that it works).
3. Images, secret values and data in AWS (sections 3 and 4).

## 0. Cost at each stage (lower bounds, from cloud-costs.md)
| Stage | What exists | Lower bound for the month |
|---|---|---|
| After the first `apply` (section 2) | Network, buckets, repositories, empty secrets, log groups, roles, cluster, services at zero tasks, budget. No endpoints. | $2.15 |
| A session of 8 hours (section 5) | Plus endpoints and both tasks for the session | $3.21 |
| Forgotten and left on | Endpoints and both tasks all month | $93.73 |
All three exclude S3, data transfer, log storage and credits (see "Not priced").

## 1. Account hygiene (before any Terraform)
1. Use an account you can afford to make mistakes in. Turn on MFA for the root user and do not create root access keys.
2. Work as a named user or role with MFA (IAM Identity Center or a role), not as root. Give it only what `terraform plan` and `apply` for this stack need (creating the resource types in `infrastructure/terraform`), not administrator access if you can avoid it. The exact permission list was not worked out (TO VERIFY).
3. Make billing data visible to your user (an account setting that must be enabled by the root user; TO VERIFY) so that you can read the bill.
4. Put credentials in a profile, never in files in this project:
```powershell
aws configure sso --profile mmrisk         # or the method your account uses
$env:AWS_PROFILE = "mmrisk"
aws sts get-caller-identity               # check the account is the one you mean; do not paste the id into the repository
```

## 2. First apply: the resting stack
```powershell
cd C:\Users\geek\projects\mobile-money-fraud-detection-platform\infrastructure\terraform
Copy-Item terraform.tfvars.example terraform.tfvars       # owner, expires, monthly_budget_usd, budget_alert_email
terraform init
terraform validate
terraform plan
```
Read the whole plan. Expected: `Plan: 52 to add, 0 to change, 0 to destroy.` (counted from the files with endpoints off; the real number is TO VERIFY) and no interface endpoints. If the plan contains a NAT gateway, a load balancer, a public IP, an internet gateway or any task above zero, stop: something changed.

Only with your explicit decision:
```powershell
terraform apply
```
Afterwards, confirm nothing runs: `aws ecs list-tasks --cluster mmrisk-cluster` (empty) and `aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values=<vpc id>` (only the free S3 gateway endpoint; its price is TO VERIFY).

## 3. Put the images in
```powershell
terraform output ecr_repositories
$region = "us-east-1"
$registry = "<account id>.dkr.ecr.$region.amazonaws.com"       # from aws sts get-caller-identity
aws ecr get-login-password --region $region | docker login --username AWS --password-stdin $registry
docker build -t mmrisk-api:0.1.0 .
docker build -f Dockerfile.dashboard -t mmrisk-dashboard:0.1.0 .
docker tag mmrisk-api:0.1.0 $registry/mmrisk/api:0.1.0
docker tag mmrisk-dashboard:0.1.0 $registry/mmrisk/dashboard:0.1.0
docker push $registry/mmrisk/api:0.1.0
docker push $registry/mmrisk/dashboard:0.1.0
docker images mmrisk-api mmrisk-dashboard           # the real sizes: put them in the cost estimate (--ecr-gb)
```
Tags are immutable: a second push to `0.1.0` is refused. Use a new version and set `api_image_tag` / `dashboard_image_tag`. Images must be `linux/amd64` (the task definition says X86_64); building on a Windows PC normally gives that.

## 4. Put the secrets and the data in
The Terraform made empty secrets. Values come from the files `deploy init` made, set by you, never by Terraform:
```powershell
cd C:\Users\geek\projects\mobile-money-fraud-detection-platform
aws secretsmanager put-secret-value --secret-id mmrisk/api_keys       --secret-string file://secrets/api_keys.json
aws secretsmanager put-secret-value --secret-id mmrisk/tls_cert       --secret-string file://secrets/tls/cert.pem
aws secretsmanager put-secret-value --secret-id mmrisk/tls_key        --secret-string file://secrets/tls/key.pem
aws secretsmanager put-secret-value --secret-id mmrisk/dashboard_hash --secret-string file://secrets/dashboard.hash
aws secretsmanager put-secret-value --secret-id mmrisk/metrics_key    --secret-string file://secrets/metrics_key
```
The data (the registry is small; the lake is the larger one; synthetic only):
```powershell
terraform -chdir=infrastructure/terraform output bucket_names
aws s3 sync models\registry s3://<registry bucket>/ 
aws s3 sync data\processed  s3://<lake bucket>/
```
Never put a real key, certificate or customer file anywhere in the repository to make this easier.

## 5. A session
Turn the endpoints on, start the tasks, work, stop. Start a timer.
```powershell
cd infrastructure\terraform
terraform apply -var endpoints_enabled=true -var desired_count=1     # shows the plan; approve it
aws ecs list-tasks --cluster mmrisk-cluster
```
Expect the tasks to reach RUNNING only after the entry script exists (section 7). Reach the private tasks by an SSM port forward through ECS Exec (TO VERIFY that this works for a Fargate task in this setup):
```powershell
aws ssm start-session --target ecs:mmrisk-cluster_<task id>_<runtime id> `
  --document-name AWS-StartPortForwardingSession `
  --parameters '{"portNumber":["8443"],"localPortNumber":["8443"]}'
```
In another window, `verify` needs no change, because the self-signed certificate already names `127.0.0.1`:
```powershell
$env:MMRISK_API_KEY = "<score key>"
.\.venv\Scripts\python -m mmrisk.deploy verify --url https://127.0.0.1:8443
```
Capture evidence for the portfolio while it runs (the `verify` output, a dashboard screenshot, the time started and stopped, the cost estimate for the session). Do not leave a browser tab or a port forward open as a reason to keep it running.

**End the session, always:**
```powershell
terraform apply -var endpoints_enabled=false -var desired_count=0
aws ecs list-tasks --cluster mmrisk-cluster                                         # empty
aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values=<vpc id> --query "VpcEndpoints[].ServiceName"   # S3 only
```
The next day, read the bill (section 6 of cloud-costs.md).

## 6. Teardown (the only state that costs nothing)
The Terraform will not delete non-empty buckets or ECR repositories (deliberate).
```powershell
aws s3 rm s3://<lake bucket> --recursive                         # repeat for registry and backups
# versioned buckets also keep old versions and delete markers: empty them in the console ("Empty bucket") or with the versions API
aws ecr batch-delete-image --repository-name mmrisk/api --image-ids imageTag=0.1.0   # and for dashboard
cd infrastructure\terraform
terraform destroy
```
Secrets stay recoverable for `secret_recovery_days` (7 by default) before they vanish; they are billed until then (TO VERIFY). Afterwards look for leftovers by tag:
```powershell
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=mmrisk
```
(TO VERIFY that the command finds all of them; some resources may not be taggable that way.) Also check `terraform.tfstate` stays out of git (it is ignored) and delete it when finished.

## 7. Specification of the missing start-up script (`/app/aws-entrypoint.sh`)
Not written: it needs an AWS client in the image, and nothing here can run it.
- **Input:** environment variables `MMRISK_SECRET_API_KEYS`, `MMRISK_SECRET_TLS_CERT`, `MMRISK_SECRET_TLS_KEY` (API) or `MMRISK_SECRET_DASHBOARD_HASH` (dashboard), `MMRISK_REGISTRY_BUCKET`, `MMRISK_LAKE_BUCKET`.
- **Steps:** create `/scratch/secrets` with mode 700, write each secret to the file the command expects (mode 600, owner 10001), copy the registry and lake from S3 to `/scratch/registry` and `/scratch/lake` (read-only credentials from the task role), create `/scratch/audit`, then `exec "$@"`.
- **Failure rules:** any missing variable, empty secret or failed copy exits non-zero before the server starts; never print a secret; never fall back to running without keys.
- **Image change:** an S3 client (the AWS CLI or `boto3`), and the script itself; `readOnlyRootFilesystem` stays on.
- **Test it** with a fake S3 first (unit), then in a real session. Until then, `desired_count` stays 0.

## 8. If a charge surprises you
1. `terraform apply -var endpoints_enabled=false -var desired_count=0`.
2. Check running tasks and endpoints (commands in section 5).
3. In the billing console, group costs by service for the days involved.
4. If unsure, tear down (section 6). It is cheaper to rebuild than to guess.

## 9. TO VERIFY (nothing above was run)
The SSO and billing-access steps; the permission set; the number in `Plan:`; the SSM port forward to a Fargate task; ECS Exec on a read-only task; the `docker login` command shape; Secrets Manager deletion billing; the tag query; whether the free S3 gateway endpoint is free; billing data delay; every price (cloud-costs.md); the Terraform arguments themselves (`validate` and `plan` have never run).
