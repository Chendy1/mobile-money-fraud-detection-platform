# Interview preparation (Phase 26)

Answers drawn from what the repository actually records. Each one names the file to point at. Where an answer would need something the project has not done, the answer says so; "I did not do that, here is what I would do" beats a bluff. Use your own words and your own "I": say only what you did, directed or checked yourself.

## First, how to answer "how did you build this?"
This project was built phase by phase with an AI coding assistant (Claude) that wrote most of the code and documents, while you set the scope and the rules, ran everything on your Windows machine, pasted back the results, and decided what to do next. An interviewer will ask, and the honest answer is also the strong one if you can back it. Suggested answer:

> "I built it with an AI coding assistant, one phase at a time. I set the rules up front: synthetic data only, nothing claimed that was not verified, and every phase ends with tests and a list of what was not checked. I ran each phase on my own machine, found and chased the failures, and I can walk through every design decision. Let me show you the decision record for the evaluation."

Then **be able to do that.** The self-test at the end of this page is the bar. Do not say "I wrote every line", do not hide the assistant if asked, and do not defend a choice you cannot explain. If a recruiter's rules require disclosure of AI use, follow them.

## Numbers to know (and where they come from)
| Fact | Value | Source |
|---|---|---|
| Dataset | 200,000 transactions, 5,000 customers, 2025 | [eda-report.md](eda-report.md) |
| Label noise | 10.7% of fraud-labelled matured rows are not truly fraud | [eda-report.md](eda-report.md) |
| Label delay | fraud reported up to 45 days late; negatives confirmed after 45 days | [ADR-012](adr/ADR-012-time-split-and-evaluation.md) |
| Test window | 24,224 transactions, 107 fraud-labelled (0.44%) | [model-card.md](model-card.md) |
| PR-AUC (test) | rules 0.161, logistic regression 0.515, random forest 0.552, LightGBM 0.502; random scorer 0.004 | [model-card.md](model-card.md) |
| How wide | about +/-0.12 on PR-AUC; the learned models are not separable | [ADR-012](adr/ADR-012-time-split-and-evaluation.md) |
| Cost per 10,000 transactions (ASSUMED costs, KES) | allow everything 70,126; fixed 0.5 cut-off 57,125; shipped policy 37,935 (46% below allow-all) | [model-card.md](model-card.md) |
| Assumed costs | loss rate 1.0 of the amount, review 25 KES, a review stops 70% of fraud, wrong block 150 KES | [ADR-013](adr/ADR-013-calibration-and-cost-based-decisions.md) |
| Features | 28, one library for training and serving | [feature-catalog.md](feature-catalog.md) |
| Drift thresholds | PSI 0.1 moderate, 0.25 major (conventions, assumed); x1.5 amounts gave 0.09 (missed), x3 gave 0.56 (flagged) | [ADR-020](adr/ADR-020-drift-detection.md) |
| Scoring speed | in-process mean 4.9 ms per score on 2 shared CPUs; about 200 scores per second at most for one process; 14.7 ms on the author's laptop (unexplained gap) | [load-testing.md](load-testing.md) |
| Load targets | p95 100 ms, p99 250 ms, errors 1% (ASSUMED, nobody set them) | [load-testing.md](load-testing.md) |
| Security checks | `deploy verify`: 12 of 12 against the container on the author's machine | [deployment.md](deployment.md) |
| Cloud cost | lower bounds per month: resting $2.15, one 8-hour session $3.21, always-on $93.73 (sourced prices, ASSUMED use, not a bill, nothing applied) | [cloud-costs.md](cloud-costs.md) |
| Tests | 1185 passed, 2 skipped in the sandbox | [verification-status.md](verification-status.md) |
| Decisions | 29 ADRs | [decisions.md](decisions.md) |

## Walkthroughs
**Two minutes (architecture).** Synthetic generator with delayed, noisy labels and the truth kept separate. Layered lake: raw is immutable, bad rows go to quarantine with a reason, a ledger records each load. One feature library, strictly backward-looking, used by training and by the API. Time split with a 45-day label gap; four scorers; Platt calibration; an expected-cost rule picks ALLOW, REVIEW or BLOCK. Model bundle in a file registry; the API loads the production version. Around it: stream simulator, monitoring checks, PSI drift, gated retraining, dashboard, load tester. Deployment: hardened compose, preflight, outside-in verifier, backup and restore. Cloud: design and plan-only Terraform. (Diagram: [architecture.md](architecture.md).)

**Five minutes (the decisions).** Pick three: the evaluation design ([ADR-012](adr/ADR-012-time-split-and-evaluation.md)), the shared feature library ([ADR-011](adr/ADR-011-shared-point-in-time-features.md)), and the cost rule with its honest result ([ADR-013](adr/ADR-013-calibration-and-cost-based-decisions.md)). For each: what the risk was, what was chosen, what it costs, what was rejected.

## Stories (what happened, what I did, what I learned)
**1. CI stayed red for five pushes.** After Phase 20 every push failed in one `docker-build` step. The cause was a `deploy` extra that listed two libraries while the deploy commands import the whole API package, so a clean install failed with `No module named 'joblib'`. It passed on my machine because my environment had everything installed. My first fix (adding `setup-python`) was a wrong guess from a step name; the real error was in the last 40 lines of the log. Fix: make the extra complete and add a test that keeps it a superset of the serving extra. Lessons: read the end of the log before theorising, reproduce in a clean virtual environment, and a red build you stop looking at hides the next break. ([verification-status.md](verification-status.md), [troubleshooting.md](troubleshooting.md))

**2. The dashboard container crashed on first run.** Same family: the image installed only `streamlit`, but the registry reader needs scikit-learn. Found on the first real container run; fixed with a dedicated extra and a regression test. I would not call the rebuilt container verified until I have run it ([deployment.md](deployment.md)).

**3. A negative result I kept.** LightGBM was chosen in advance (ADR-002) and did not beat the random forest on PR-AUC (0.502 vs 0.552); the paired difference includes zero. I reported that, kept LightGBM for exact explanations and native handling of missing history, and said the cost ordering changes with the assumed costs. ([model-card.md](model-card.md))

**4. A measured bug in the drift statistic.** Interpolated bin edges made an empty bin when many values tie, which distorts PSI; the fix was to use observed values as cut points. Measurement also showed that some features move for reasons that are not drift (calendar position, accumulating counters) and that the detector misses a 50% rise in amounts. All three are documented rather than hidden. ([ADR-020](adr/ADR-020-drift-detection.md))

**5. The "free when idle" cloud design was not free.** Pricing the design showed the interface VPC endpoints bill by the hour even with no task running. Response: gate them behind a variable (default off) and make Terraform refuse to start a service without them; the estimator reports lower bounds with what it did not price. Nothing was applied. ([ADR-028](adr/ADR-028-cloud-cost-controls.md))

**6. A test of my tests.** Mutation checks (deliberately breaking code and confirming a test fails) found a surviving mutant in the demo: nothing checked that a failed security check turned the step red. I closed it with a test. ([testing.md](testing.md))

## Questions and answers
### Data and evaluation
**Why a time split with a gap, not k-fold?** Fraud labels arrive late, and one fraud episode spans many rows, so a random split puts the same episode on both sides and trains on labels that would not yet exist. Split by time, leave 45 days between training and validation so training labels were knowable on the deployment date, and never evaluate the last 45 days. It discards about 40% of the year; that is the price. ([ADR-012](adr/ADR-012-time-split-and-evaluation.md))

**How do you know there is no leakage?** Features use only rows strictly before the current one (timestamp, then transaction id) and take no labels or whole-dataset statistics. Evidence: truncation tests (cutting the future must not change a feature), an independent DuckDB definition cross-check, a single-feature AUC audit, and online/offline parity. Caveat: I looked at test scores during development; no setting was changed on that basis. ([ADR-011](adr/ADR-011-shared-point-in-time-features.md), [ADR-012](adr/ADR-012-time-split-and-evaluation.md))

**Why PR-AUC and review budgets, not accuracy or ROC-AUC?** At 0.44% fraud, accuracy is meaningless and ROC-AUC flatters. PR-AUC's baseline is the fraud rate, and precision and recall at a fixed number of alerts per 10,000 matches how a review team works.

**Why bootstrap by customer?** A customer's transactions are not independent, and neither are episodes; row-level resampling gives intervals that are too narrow.

**Your test set has 107 frauds. Can you trust the ranking?** No, and the report says so: the intervals overlap and I do not claim a ranking. I would repeat the run across several seeds before calling any number typical ([limitations.md](limitations.md)).

**Labels are noisy. What did you do?** Measured it (10.7% of fraud-labelled matured rows are not fraud; some real fraud is never reported) using the generator's hidden truth, kept that truth out of training, and shown the audit separately. In reality you cannot measure it this way; you would sample and investigate.

### Modelling and decisions
**Why calibrate?** A score ranks; a cost rule needs a probability. Platt scaling (two parameters) is fitted on validation only because isotonic needs more positives than the window has.

**Explain the decision rule.** Expected cost per action: ALLOW costs p x loss x amount; REVIEW costs the review plus the fraud a review misses; BLOCK costs (1 - p) x friction. Lowest wins, ties go to the less severe action. Larger amounts therefore get a lower threshold. It beat a fixed 0.5 cut-off (37,935 vs 57,125) under the assumed costs.

**Are those costs real?** No. All four are assumptions, printed as such, with a sensitivity table (0.25x to 4x) because the ranking of scorers changes with them. The point is the method and the sensitivity, not the money.

**Why LightGBM if it did not win?** See story 3. Honest answer: it was chosen up front, the data cannot separate it from the alternatives, and it gives exact explanations and handles missing history natively.

### Serving and MLOps
**How do you prevent training/serving skew?** There is one implementation. The online path builds a small frame from the customer's history plus the new transaction and calls the same function the batch path uses; a test requires exact agreement and the demo shows a zero difference over 300 held-out transactions. Cost: it recomputes per request, fine here, not at scale; history is unbounded in memory ([ADR-011](adr/ADR-011-shared-point-in-time-features.md)).

**What does monitoring cover, and what does it not?** It checks decisions (integrity, gaps, action matches risk level, reasons present) and input drift (PSI). With labels up to 45 days late it cannot see correctness; it says so. The Prometheus and Grafana containers were never run, and there is no alert delivery ([monitoring.md](monitoring.md)).

**How does retraining decide?** Triggers: major drift, the champion's data ending more than 90 days before the end of the lake (assumed), 200 or more new fraud labels, or forced; a missing input is shown as `unknown`, never as "no". A candidate must pass gates against the champion, is only registered, and a person promotes it with a reason ([ADR-021](adr/ADR-021-gated-retraining.md)).

**Why no Kafka, Kubernetes, Feast, Airflow?** One machine, one model, 8 GB of memory. Each would add operations without adding a lesson here; the alternatives are recorded in the ADRs ([ADR-003](adr/ADR-003-in-process-queue.md), [ADR-005](adr/ADR-005-compose-not-kubernetes.md)). I would revisit with a concrete need.

**How does your load test differ from a naive one?** It is open-loop: requests are issued on a schedule, and latency counts from when each was due, so a slow server cannot hide its queue by slowing the client. Limit: one run on one machine; the 4.9 ms versus 14.7 ms per-score gap between machines is unexplained ([ADR-024](adr/ADR-024-load-testing.md)).

### Security
**What protects the API?** Hashed API keys with roles, TLS, per-key rate limits (600 a minute, burst 50, assumed) and a failure counter per address, strict input schema with a size limit, an audit log that never stores keys, a loader that refuses code in model files. The verifier checks the boundaries from outside. It is not a security audit; the threat model lists what is missing (no key expiry, no request signing, a model-file loader that is not a sandbox) ([security.md](security.md), [threat-model.md](threat-model.md)).

**Anything you got wrong in the docs?** Yes: while preparing this phase I found the threat model still said "no request rate limit" after Phase 18 added them (they apply only when keys are on), and still said Terraform had never been validated after CI ran `validate`. Both are corrected, and a test now fails if those stale sentences return.

### Cloud
**Did you deploy to AWS?** No. It is a design, plan-only Terraform that passes `validate` in CI, and a cost estimator; `plan` and `apply` were never run, and there is a known gap (the container entrypoint script is not written). I can explain every choice: private subnets and no public endpoint, S3, ECR, Secrets Manager with empty containers, IAM with named resources, the endpoint gating. ([aws-design.md](aws-design.md), [terraform.md](terraform.md))

**What would it cost?** Lower bounds from sourced prices: about $2.15 a month resting, $3.21 with one 8-hour session, $93.73 if left on, under assumed use; not a bill, and S3, data transfer and some log costs are not priced.

### Testing and process
**How do you know the tests test anything?** Mutation checks: break the code on purpose and require a named test to fail. Several found real gaps (story 6). They are a check on the tests, not a proof of correctness.

**What was your process?** One phase at a time: build, test, verify, update the docs, list what was not checked, stop. Every claim is labelled verified or TO VERIFY, and numbers come from commands, not memory ([verification-status.md](verification-status.md)).

## Hard questions
- **"Is this production-ready?"** No. It is a local simulation on synthetic data. What it has that production needs: tested boundaries, backups, a preflight. What it lacks: real data, high availability, alert delivery, image scanning, a cloud deployment.
- **"Would this catch real fraud?"** Unknown, and the data cannot tell us. The fraud patterns are ones the generator writes, so some features (inbound-transfer features) are optimistic compared with real data ([ADR-011](adr/ADR-011-shared-point-in-time-features.md)).
- **"What is the weakest part?"** The evaluation rests on about a hundred frauds and one seed; the performance numbers are one run on shared CPUs; several pieces were never run end to end (Prometheus and Grafana, Terraform plan, the demo on Windows).
- **"What would you do next?"** Repeat the evaluation across seeds, run Prometheus and Grafana, write the container entrypoint and run a Terraform plan with read-only credentials, explain the laptop-versus-sandbox latency gap, and test on a public fraud dataset to see how the cost rule behaves on data I did not write.
- **"Why synthetic data?"** Real mobile-money data is private and regulated; synthetic data lets the pipeline be built, tested and shared safely, with the limits stated ([ADR-001](adr/ADR-001-synthetic-data.md)).

## Questions to ask them
- How do you get fraud labels, and how late are they? How do you evaluate before they arrive?
- Who owns the cost of a false review versus a missed fraud, and has anyone written those costs down?
- How do you decide to retrain and who approves a promotion?
- What does an on-call week look like for this system?

## Self-test (do these without notes before an interview)
1. Draw the architecture in two minutes and name what each box reads and writes.
2. Explain the split dates and why there is a gap, with the 45-day numbers.
3. State the decision rule and what changes if the review cost doubles.
4. Explain why LightGBM is shipped though it did not win.
5. Walk through the feature library and name one feature and how it avoids the future.
6. Run `.\scripts\dev.ps1 demo` and narrate each step without reading the screen text.
7. Tell story 1 in ninety seconds, including the wrong guess.
8. List five things the project does not do (use [limitations.md](limitations.md)).
9. Answer "how did you build this?" in your own words, including the assistant.
