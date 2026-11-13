# Demo scripts (Phase 25)

A guided demo that runs the project's own commands in a fixed order, tells you what to say at each step, and checks that each command's output shows what the step claims. It needs no Docker, no browser and no internet. It writes only under `data/demo/` (`dev.ps1 demo-clean` deletes it). Design: [ADR-029](adr/ADR-029-guided-demo.md).

**What it is not.** It is not a recording and it invents nothing: every line on screen is read from a command just run. If a step's output does not contain what it should, that step is marked `UNEXPECTED` and the demo says so; it never prints a green tick for output it did not check. The data is synthetic and the results say nothing about real fraud ([limitations](limitations.md)).

## Before you start
You need what the earlier phases produce: the cleaned lake, a trained model, a registry with a production model, and the model card. If you have run the project from the README these exist. Check:
```powershell
cd C:\Users\geek\projects\mobile-money-fraud-detection-platform
.\scripts\dev.ps1 demo-check
```
Expected: five lines starting `found`. A line starting `MISSING` ends with the command that makes it (`dev.ps1 generate`, `ingest`, `advanced`, `regboot`, `card`). The model card is the committed one, written by the run that produced it; if you retrain, regenerate it (`dev.ps1 card`) or the table in step 2 and your model will disagree.

## The two scripts
```powershell
.\scripts\dev.ps1 demo         # the short track: 6 steps, press Enter between them
.\scripts\dev.ps1 demo-full    # every step: 12 steps
.\.venv\Scripts\python -m mmrisk.demo run --yes                 # no pauses (a dry run before the audience)
.\.venv\Scripts\python -m mmrisk.demo run --only apicheck,secure # chosen steps, in script order
.\.venv\Scripts\python -m mmrisk.demo list --track short        # the talking points, nothing run
```
Exit code 0 means every step ran and showed what it should; 1 means a step failed or its output was not what the step expects. **Do a `--yes` dry run first, on the machine you will present on.**

Timings below were measured once in the assistant's 2-CPU Linux sandbox (a model trained with `--quick`; the whole 12-step run took 37 s). Per-score time on the author's Windows machine was about three times slower in Phase 19, so allow two to four times longer there. The demo itself has **not** been run on Windows (TO VERIFY): the `dev.ps1` tasks were not run in PowerShell by the assistant, and `python -m mmrisk.demo ...` is the same code without them.

### Script A: five minutes (`demo`)
Opening, 30 seconds, in your own words: *"This is an end-to-end fraud-risk scoring platform on synthetic mobile-money data. I will show five things: how it reports its result honestly, that online scoring equals batch scoring, that its security boundaries are tested, that it notices a data shift, and what the cloud design would cost. Nothing here runs in the cloud."*

| # | Step id | Show | Say (the point of the step) |
|---|---|---|---|
| 1 | `prereq` | the ground is in place | nothing to say; it avoids a failed demo |
| 2 | `card` | the model-card results table | Synthetic data; 107 frauds in the test window so intervals are wide; LightGBM did **not** beat the random forest on PR-AUC and is shipped for exact explanations and missing-history handling; costs are ASSUMED |
| 3 | `apicheck` | `largest probability difference ... 0.00e+00`, `same action ... True` | the API runs the same feature code as training; latency is one process on this machine, not a capacity claim |
| 4 | `secure` | `12 of 12 checks passed` | a real TLS + API-key server; each line is a tested boundary (no key, wrong key, role limits, plain HTTP); not an audit |
| 5 | `drift_shift` | `MAJOR amount_kes ... DRIFT DETECTED` | the same detector, amounts multiplied by 3; exit code 1 is the point; a 50% rise was measured to go unnoticed (ADR-020) |
| 6 | `cost` | `TOTAL (lower bound) $2.15` | nothing was created in AWS; prices are sourced, a lower bound, with what is not priced listed under it |

Closing, 20 seconds: *"What I would not claim: real-world performance, a measured capacity, or a cloud deployment. What I would claim: the evaluation is leak-safe and honest about its uncertainty, the same code scores online and in batch, and the boundaries are tested rather than asserted."*

### Script B: fifteen minutes (`demo-full`)
Script A plus these, in this order (the runner puts them in script order):

| Step id | Where it fits | Show | Say |
|---|---|---|---|
| `quality` | after `prereq` | `OVERALL: PASS (PASS 63, WARN 0, FAIL 0, SKIPPED 1)` | bad rows are quarantined with a reason; 63 dataset checks with written thresholds; freshness skipped because this is a replay |
| `simulate` | after `secure` | `balanced: every event is accounted for` | a replayed feed with seeded faults (jitter, repeats, corrupted copies) through a bounded queue; every event ends scored, rejected or dropped |
| `monitor` | after `simulate` | ten `PASS` lines | checks on stored decisions: integrity, gaps, action vs risk level, reasons on flagged rows; they see decisions, **not** correctness (no labels yet) |
| `drift` | before `drift_shift` | `no drift detected` for a normal week | PSI against the validation window; features that grow with time are listed, not counted, on purpose |
| `retrain` | after `drift_shift` | four triggers, `no retrain needed now` | retraining is a question with rules (drift, age over 90 days, 200 new labels); a candidate is only registered and a person promotes it |
| `dashcheck` | before `cost` | `health: PASS`, `models: 1 versions, production v0001` | the dashboard is read-only over these files |

## Recorded sample output
From the sandbox run described above. Your timings and the flagged share will differ because your model was trained on your machine; the markers the runner checks for (`OVERALL: PASS`, `check: PASS`, `balanced`, `DRIFT DETECTED`, `TOTAL (lower bound)`) will be the same.

```text
quality     OVERALL: PASS  (PASS 63, WARN 0, FAIL 0, SKIPPED 1)
apicheck    scored 300 held-out transactions after replaying 199700 earlier ones
              largest probability difference from batch scoring: 0.00e+00
              same action as batch scoring: True
              latency per transaction (this machine, one process): p50 4.1 ms, p95 9.5 ms
secure      PASS  no key: /v1/model is refused  401
            PASS  a score key cannot read /metrics  403
            PASS  a metrics key cannot score or describe  403
            PASS  plain HTTP is refused  no clear-text answer
            12 of 12 checks passed (history replay skipped to start quickly)
simulate    events in 2035 {'normal': 2000, 'duplicate': 18, 'invalid': 17}
              scored 2000 ... errors 0;  actions {'ALLOW': 1957, 'REVIEW': 34, 'BLOCK': 9}
            balanced: every event is accounted for
drift_shift MAJOR     amount_kes   PSI  0.558
            4698 rows over 7.0 days; DRIFT DETECTED: major shift in amount_kes, amount_log, amount_to_avg_30d
retrain     no retrain needed now
```
The full 12-step run in the sandbox: all steps `OK`, 37 s in total.

## If something goes wrong
| What you see | Meaning | Do |
|---|---|---|
| `MISSING ...` in `prereq` | a step before the demo was not run | run the command printed after `fix:` |
| a step is `FAILED` with `(exit code N; expected ...)` | the command itself failed; its output is shown | read the last lines; the usual causes are a port in use (`secure` picks a free one) or a missing library (`pip install -e ".[dev,eda,ml,explain,tracking,api,dashboard,deploy]"`) |
| a step is `UNEXPECTED: output lacks ...` | the command ran but did not show what the step claims | do not present it as a pass; look at the output, then run `dev.ps1 demo-clean` and retry |
| `SKIPPED: needs simulate first` | a step it builds on failed | fix the earlier step first |
| `secure` says the API was not ready | the server did not start in 90 s | the error text shows the server's own log tail |
| the table in step 2 disagrees with your model | the card is from the run that wrote it | regenerate with `dev.ps1 card` |

## What the demo does not show (and why)
These need something the runner cannot assume, so they are talking points or optional extras, **not** verified parts of the demo:
- **The dashboard in a browser** (`dev.ps1 dashboard`, http://127.0.0.1:8501): needs Streamlit and a browser; its tabs and masking have never been viewed by the assistant (TO VERIFY). Step `dashcheck` shows what it would read.
- **The production-like Docker stack** (`dev.ps1 up-prod`, then `deploy verify`): needs Docker Desktop and a minute or two to become healthy. The author ran it: 12 of 12 checks passed.
- **Terraform** (`dev.ps1 tf-validate`): needs the Terraform CLI; passes in CI; `plan` and `apply` have never been run.
- **Load testing** (`dev.ps1 loadtest`): roughly a minute or two, and the result depends on the machine; see [load-testing.md](load-testing.md) before quoting any number.

## Things not to say
- That any metric describes real fraud, real customers or real money.
- That LightGBM beats the other models; the test window cannot separate them.
- That the costs are real; they are ASSUMED, and the AWS figures are lower bounds from page prices.
- That anything was deployed to AWS, or that the dashboard has been checked in a browser.
