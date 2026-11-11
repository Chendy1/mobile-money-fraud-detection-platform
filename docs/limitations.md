# Limitations and open items

Read this before reading any number in the repository. Each limit says what it changes about how a result should be read.

## What the project is not
- **Not evidence about real fraud.** All data comes from the repository's own generator. The fraud patterns are ones the author wrote, so a model that finds them shows the pipeline works, not that it would find real fraud. ([ADR-001](adr/ADR-001-synthetic-data.md))
- **Not affiliated with Safaricom or M-Pesa.** "M-Pesa-inspired" describes the domain, nothing more.
- **Not a security audit, not high availability, not a cloud deployment.** The production-like deployment runs on one computer. Nothing was created in AWS.

## Evaluation limits
- **Few frauds in the test window.** The test period holds 107 fraud-labelled transactions (0.44%). Intervals are wide: the three trained models are not statistically separable on PR-AUC, and LightGBM did **not** beat the random forest on the point estimate (0.502 vs 0.552). It is the shipped model for reasons other than rank ([model card](model-card.md)).
- **Labels are noisy and late by design.** About 10.7% of fraud-labelled rows in the default dataset are not truly fraud (audit in [eda-report.md](eda-report.md)). The last 45 days are never evaluated.
- **One dataset, one seed.** Headline numbers are for seed 42 and the default size. They were not repeated across seeds, so seed-to-seed variation is unmeasured.
- **Costs and thresholds are assumptions.** Review cost, block friction, loss rate, drift thresholds, monitoring thresholds, retraining gates, load targets and container limits are labelled ASSUMED where they appear. Savings figures follow from them and mean nothing in money. ([cost-model.md](cost-model.md))

## Operational limits
- **Single process, in-memory history.** The scoring API holds one lock and replays customer history at start (about a minute or two). A restart drops state and replays it; there is no failover. In-process scoring measured about 200 scores per second at most.
- **Performance numbers are narrow.** Latency and load figures come from one run each on 2 shared CPUs. On the author's Windows machine (4 CPUs) one run held the targets only up to 25 requests/s and in-process scoring took 14.7 ms per score, against 100 requests/s and 4.9 ms in the sandbox; the gap was not investigated. No long runs, no container measurements, no several-process measurements. ([load-testing.md](load-testing.md))
- **The model registry is files.** Anyone who can write the registry folder can change the model. The loader refuses code in model files but is not a sandbox. ([security.md](security.md))
- **Retraining stays on the host**, by design, and promotion needs a person.
- **Self-signed TLS** protects the API connection on one machine because clients trust that one file. The dashboard is plain HTTP on loopback.
- **Windows is the author's platform, but most phases have no reported Windows full-suite run.** See [verification-status.md](verification-status.md).

## Cloud limits
- **Plan-only.** Terraform passes `fmt`, `init` and `validate` in CI. It has never been planned or applied, and applying is not part of this project.
- **Costs are lower bounds** from prices read on AWS pages (many are page examples, not confirmed table rates); items that were not priced are listed in [cloud-costs.md](cloud-costs.md). The interface endpoints bill hourly even with no task running, which is why they are gated.
- **A known gap:** the ECS task definitions point at an entrypoint script that does not exist yet.

## Open items (not done)
1. Dashboard tabs and masking in a browser; rebuilt dashboard container on the author's machine.
2. Prometheus and Grafana profile run; permissions on Linux hosts.
3. `pip-audit` findings in mlflow, pillow and pyasn1 (versions seen in the sandbox) not upgraded; image scanning and digest pinning not done; no Alertmanager.
4. `/app/aws-entrypoint.sh` not written; Terraform `plan` never run.
5. Older-library test set not re-run after Phase 21.
6. Slow Windows test suite (cause not investigated).
7. CI Node.js 20 deprecation warnings: bump the pinned action versions.
8. Repeat the evaluation across several seeds before quoting any single number as typical.
