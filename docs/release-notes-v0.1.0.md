# v0.1.0: first release

A local-first, M-Pesa-inspired fraud-risk scoring platform, built phase by phase on **synthetic data**. It is a portfolio project: nothing here has seen a real customer, a real transaction or a cloud account, and it is not affiliated with Safaricom.

## What is in it
- A synthetic generator with delayed, noisy labels and the hidden truth kept apart; a layered data lake with quarantine and two-tier data-quality checks; 28 point-in-time-safe features shared by training and serving.
- A leak-safe evaluation (time split with a label gap, customer-level bootstrap intervals), calibrated LightGBM, and an ALLOW, REVIEW or BLOCK rule from expected cost under **assumed** costs.
- A FastAPI scoring service with hashed API keys, roles, TLS, rate limits and an audit log; a stream simulator; drift detection; gated retraining with a file-based registry; a read-only dashboard; an open-loop load tester.
- A hardened Docker Compose deployment with a verifier, backup and restore; GitHub Actions with four jobs; a guided demo that checks its own output.
- A plan-only AWS design with Terraform that passes `terraform validate`, and a cost estimator that uses prices read from AWS pages.
- 29 architecture decision records and more than 1,200 automated tests.

## What it does not show
- **No real-world performance.** With 107 fraud-labelled transactions in the test window, the learned models cannot be separated; LightGBM did not beat the random forest.
- **Nothing was deployed to AWS.** Terraform was validated, never planned or applied.
- **Costs and thresholds are assumptions.** The 46% cost reduction holds only under the stated assumed costs.
- **Not a security audit**, not high availability, no capacity claim beyond one process on two machines.

## How much of it was checked, and where
The [evidence scorecard](scorecard.md) places 23 areas on a ladder. At this release 12 are at E4 (run on the author's machine after their last change), 11 at E3 (tests green in CI), and none at E5 (checked against something the project did not write; the data is synthetic by design). The ladder is the author's own design. [verification-status.md](verification-status.md) says what ran in CI, on the author's Windows machine and in the assistant's sandbox, and what did not run at all (the Prometheus and Grafana containers, the dashboard in a browser, the demo on Windows, Terraform `plan`).

## Built with an AI coding assistant
Most of the code and documents were written by Claude under the author's direction: the author set the scope and rules, ran each phase on a Windows machine and decided what came next.

## Try it
Start at the [README](../README.md), then `.\scripts\dev.ps1 demo-check` and `.\scripts\dev.ps1 demo` (see [demo.md](demo.md)). Known gaps: [limitations.md](limitations.md).

Licence: Apache 2.0.
