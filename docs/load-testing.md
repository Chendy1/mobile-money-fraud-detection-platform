# Load testing (Phase 19)
How the API is loaded, how to read the result, and what the sandbox measurements do and do not say. Design: [ADR-024](adr/ADR-024-load-testing.md). Synthetic data only.

## Commands
```powershell
.\scripts\dev.ps1 loadtest       # starts the API itself, loads it in steps, stops it, writes a report
.\scripts\dev.ps1 inproc         # times scoring with no HTTP at all
.\.venv\Scripts\python -m mmrisk.loadtest bench --auth --profile spike --rate 20 --peak 150 --step-seconds 10 --last-days 14
.\.venv\Scripts\python -m mmrisk.loadtest show data\loadtest\<name>.md
```
Reports go to `data\loadtest\<name>.json` and `.md` (the folder is ignored by Git).

`bench` starts `python -m mmrisk.api serve` on a free local port with `--history-until` set to the start of the load stream, so every run begins from the same state. `--auth` runs it with API keys (a temporary key, rate limit set very high so the limit does not interfere; give `--rate-per-minute 60 --rate-burst 5` to see the limiter instead). `run` loads an API you started yourself: it needs `--from <the same time as serve --history-until>`, takes the key from the environment variable `MMRISK_LOADTEST_KEY` (not a flag, so it stays out of shell history), and changes that API's state, so restart it before another run. Only loopback addresses are loaded unless you add `--allow-external`, for a service you own.

## Profiles
`constant` (one rate), `step` (rates that rise, one segment each: finds the limit), `ramp` (rises evenly), `spike` (base, peak, base: shows recovery). Arrivals are `even` or `poisson` (seeded). Guards, ASSUMED: at most 2000 requests/s and 900 s. A profile needs one stream transaction per request; `--last-days` widens the stream (the default synthetic lake has about 330 transactions a day).

## How it measures
- **Open loop.** Requests are scheduled in advance. If the server slows down, the generator does not slow down with it.
- **Latency from the intended start.** If a worker is still waiting for a slow answer when its next request was due, that request starts late and the lateness counts as latency. A tool that times only from the moment of sending would show a flattering picture exactly when the server is struggling (coordinated omission). `service p95` is the time from sending; `late-start p95` is how late requests began.
- **Per-customer order.** The API refuses a customer's transaction older than the last one it saw (409). A transaction is therefore sent by one worker chosen from its customer, in stream order. Any 409 in a report is flagged: the run was not measuring normal scoring.
- **Warm-up.** The first `--warmup` (20) requests go one at a time and are not counted.
- **Cross-check.** After a run the API's own count of `200` answers on `/v1/score` (from `/metrics`) is compared with the client's count. In the sandbox runs they agreed exactly.
- **Achieved rate** is answers divided by the time until the last one arrived, so a backlog lowers it even when every request succeeds.
- **Targets (ASSUMED, nobody set them):** p95 ≤ 100 ms, p99 ≤ 250 ms, errors ≤ 1 %, achieved ≥ 95 % of offered. Change with `--p95-ms`, `--p99-ms`, `--max-error-rate`. `--fail-on-slo` makes a miss exit 1, for a gate.
- **Knee:** the highest rate reached before the first segment that misses the targets. If none misses, the report says the limit was not found.

## Sandbox measurements (2026-11-04)
Machine: the build sandbox, **2 CPUs**, Linux, Python 3.13, the newest library set. The load generator and the API shared those two CPUs. 8 workers, even arrivals, 8 s per step, 20 warm-up requests, synthetic data, model `models/advanced` (LightGBM, 300 rounds). One run each. Not a statement about your machine, and with one run per row, differences of a millisecond or two are noise.

| offered/s | no keys: p50 / p95 / p99 ms | keys on: p50 / p95 / p99 ms |
|---:|---|---|
| 10 | 10.9 / 13.4 / 15.5 | 11.0 / 13.8 / 20.0 |
| 25 | 10.4 / 13.4 / 17.1 | 9.9 / 13.0 / 15.3 |
| 50 | 9.8 / 12.0 / 19.3 | 8.7 / 10.6 / 11.8 |
| 75 | 8.5 / 11.4 / 17.9 | 8.9 / 11.2 / 17.6 |
| 100 | 9.2 / 14.4 / 17.8 | 8.7 / 13.3 / 15.7 |
| 125 | 444 / 1805 / 2025 (achieved 98.7/s) | 337 / 1618 / 1826 (achieved 100.7/s) |
| 150 | 1697 / 4163 / 4850 (achieved 109/s) | 2048 / 4478 / 5169 (achieved 105/s) |

- In both runs the targets held up to 100 requests/s and failed at 125. Above that the API still answered every request correctly (no errors, 4,280 of 4,280 ok) but fell behind: the answer time of a single request stayed at 80 ms (p95), while requests waited seconds to be taken up.
- Keys on made no difference that these runs can resolve: the key check, role check, rate bucket and the failure limiter add work that is small next to a score. The audit log is written only on refusals.
- Spike (20, then 150, then 20 per second, 10 s each): the first and the peak segments behaved as above; in the final 20/s segment p50 was 12 ms but p95 2.9 s, because the backlog built during the peak was still draining (offered 20/s against about 100/s capacity: the queue empties in seconds, not instantly).
- In-process scoring (`inproc`, 500 transactions, no HTTP): mean 4.9 ms, p95 7.4 ms, p99 10.5 ms. The service holds one lock, so scoring is one at a time: about 200 per second is the most one process could do even with free HTTP.
- The observed limit (about 100/s) is about half that ceiling, and close to the 123/s over HTTP that Phase 13 measured in the same sandbox (ADR-018). Why was **not isolated**: HTTP parsing, validation, metrics, JSON and the load generator all ran on the same two CPUs. Splitting them (generator on another machine) and profiling the server would tell. TO VERIFY.

## What this does not show
Other machines (your laptop will differ); the effect of customer history length (each score recomputes over that customer's history, so latency should grow as histories grow: the stream here is a week of a year-long lake and was not stretched); long runs (nothing longer than about a minute was run: memory growth, the 10,000-entry duplicate cache and log growth over hours are unmeasured); several server processes (the service state is in one process, so more workers would need shared state: not built); the container (the Docker image was not loaded); a network between generator and server; real traffic shapes (arrivals are even or Poisson; the stream's customers and amounts are the synthetic ones); failure of the model file or disk under load.

## Reading a report quickly
Look at `meets`, then `late-start p95`. A large late start with a small `service p95` means the server could not take requests as fast as they came (a queue); a large `service p95` means each request itself was slow. `errors` with `rate_limited` is the Phase 18 limiter working; `conflict` means the run was wrong; `network` means the API stopped answering (a worker gives up after 20 in a row and the report says how many requests were never sent).
