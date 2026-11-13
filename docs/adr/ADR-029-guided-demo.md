# ADR-029: A guided demo that runs the real commands and checks what they show
Status: accepted (Phase 25). Documents: [demo.md](../demo.md).

## Context
The project is shown to people who will not read 1,100 tests. A demo has to be repeatable, short, and honest. The usual failure modes are a demo that breaks on stage, a slide-like script that shows numbers nobody ran, and a "green" demo that never checked what it displayed. The author works in PowerShell on a Windows laptop; the assistant could only run Linux.

## Decisions
- **A small Python module, not a PowerShell script.** `python -m mmrisk.demo list | check | run | clean` runs the same on every platform and could be tested in the assistant's sandbox, where PowerShell is not available. `dev.ps1` and the Makefile only start it (`demo`, `demo-full`, `demo-check`, `demo-clean`). The central `dev.ps1` parameter binding was left as it was, because a mistake there would break every task and could not be tested here.
- **The script is data.** Each step is a record: the commands, the talking points, the exit codes that count as success, and regular expressions the output must match. `docs/demo.md` is written from the same steps, and a test checks that every step id appears in it.
- **Output is checked, not just exit codes.** A step whose command exits 0 but does not show its marker (for example `balanced: every event is accounted for`) is reported `UNEXPECTED`, and the run exits 1. A step that is meant to fire a detector (`drift_shift`) accepts only exit code 1.
- **Nothing on screen is typed by hand.** The results table is copied from the model card, character for character; everything else is command output.
- **Writes only under `data/demo/`.** Outputs go to demo paths, the quality step runs with `--no-write`, and a test snapshots the repository, the lake and the model folder before and after a real run. `demo clean` refuses to delete anything but `<repo>/data/demo`.
- **The security step starts a real server.** It makes a temporary secrets folder, starts the API with TLS and keys, and runs the same `deploy verify` checks used against the container, then stops the server and deletes the folder. It does not need Docker. It skips the history replay to start in seconds, and says so.
- **Dependencies between steps are explicit.** A step whose prerequisite failed is `SKIPPED`, not run on stale files.
- **Two tracks.** Short (6 steps) and standard (12); `--only` picks steps. Timings are measured once and labelled as such.

## Rejected
- A screen recording or slides with pasted results: they cannot be checked and go stale.
- Docker, the browser and Terraform inside the runner: each needs something the runner cannot assume. They are listed in the guide as talking points or optional extras, with their verification status.
- A PowerShell-only runner: untestable by the assistant, and a second implementation to keep in step.
- Passing arguments through `dev.ps1` (`ValueFromRemainingArguments`): untested change to the central script for a small convenience; the module can be called directly.

## Consequences
The demo can fail loudly, which is the point. It has been run end to end in the sandbox (12 of 12 steps, 37 s, against a model trained with `--quick`) and its real commands are exercised by an integration test in CI. It has **not** been run on Windows or through the `dev.ps1` tasks; that is TO VERIFY. It shows what the project computes on synthetic data and claims nothing beyond that.
