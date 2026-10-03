# Threat model (initial)
Scope: a local, synthetic-data simulation. No real customer data, no production exposure.

| Threat | Risk | Mitigation (planned phase) |
|---|---|---|
| Secrets committed to Git | Credential leak | `.env.example`, `.gitignore`, secret scan in CI (1, 18) |
| Malicious or malformed API input | Crash, bad scores | Pydantic validation, size limits (11) |
| Unauthorized API use | Abuse | API-key auth (11, 18) |
| Bad or corrupted data enters pipeline | Wrong model | Validation gates, quarantine (4) |
| Duplicate or replayed events | Double counting | Idempotency keys (13) |
| Model drift or poisoned retraining data | Degraded decisions | Drift monitoring, quality gates, manual approval (15, 16) |
| Vulnerable dependencies or images | Supply chain | Dependency and container scans (18) |
| Real data accidentally added | Privacy breach | Synthetic-only policy, data README, review (all) |
| Cloud misconfiguration or cost overrun | Charges, exposure | Plan-only Terraform, IaC scanning, budgets, destroy steps (22, 23) |
| Model explanations leak sensitive attributes | Privacy | Data minimization, no PII in features (6, 9) |
