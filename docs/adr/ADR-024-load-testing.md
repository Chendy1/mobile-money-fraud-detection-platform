# ADR-024: Load testing
Status: accepted (Phase 19). Usage and measurements: [load-testing.md](../load-testing.md).

## Context
Phases 11 to 18 made the API correct, observable and protected, but nothing said how much load it takes or what happens past that. Existing tools (Locust, k6, wrk) are fine but each is a new install and none knows the API's rule that a customer's transactions must arrive in order.

## Decisions
- A small tool in the repository, standard library only (threads and http.client), so it needs no install and its behaviour is covered by tests like the rest. Cost: it is a Python generator and tops out well below a dedicated one; at the rates this API can take (about 100 per second in the sandbox) that is not the limit, but it shares the machine's CPU with the server, which the report states.
- Open loop with latency from the intended start. A closed loop (each worker waits for an answer, then sends) cannot offer more than the server can take and reports only good news near saturation.
- One worker per customer, requests in stream order: avoids 409s that would test the error path instead of scoring. Duplicates and conflicts are detected and warned about.
- `bench` starts its own server on a free port with a fixed history cut, so runs are comparable and cannot hit a stale state. Separate `run` for a server you manage.
- Only loopback unless `--allow-external`; rate and duration caps. A load generator is also a denial-of-service tool.
- The key is read from an environment variable, not an argument.
- Targets are ASSUMED and printed on every report; the knee is defined against them, never against a hidden number. `--fail-on-slo` exists but CI does not run `bench`: timings on shared runners are noisy and a gate on them would fail for reasons unrelated to the code.
- A cross-check against the server's own Prometheus counter catches a generator that miscounts.
- No claim that the sandbox numbers carry over. The docs say what was and was not measured.

## Consequences
Finding: scoring is serialised by one lock in `ScoringService` and the API runs one process, so capacity is bounded by one core. The sandbox limit (about 100 per second on two shared CPUs) is about half the in-process ceiling; the cause was not isolated. No code was changed to raise capacity in this phase: that needs a measured cause first. Tests: unit (plans, statistics, worker timing with a fake clock, the real sender against a local server, report) and integration (a real API process: clean run, keys on, 429s under a tight limit, duplicate detection on a second run, no server, in-process timing). 15 mutations to the load tool were caught.
