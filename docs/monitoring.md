# Monitoring guide
Two layers, for two questions. All thresholds are assumptions for a synthetic simulation. Design: [ADR-019](adr/ADR-019-monitoring.md).

## 1. Is the service working? (Prometheus and Grafana)
```powershell
$env:GRAFANA_ADMIN_PASSWORD = "<choose one; not stored anywhere>"
.\scripts\dev.ps1 upmon          # api + prometheus + grafana
```
Prometheus: http://127.0.0.1:9090 (Status > Targets, Alerts). Grafana: http://127.0.0.1:3000, user `admin`, the dashboard "Mobile money risk scoring (synthetic data)". Send traffic with the simulator (`docker compose --profile simulate up simulator`) or `python -m mmrisk.api smoke`. Stop with `.\scripts\dev.ps1 down`. Not run by the assistant: TO VERIFY.

| Metric | Type | Meaning |
|---|---|---|
| `mmrisk_http_requests_total{endpoint,method,status}` | counter | Requests; `endpoint` is the route (`/v1/score`) or `unmatched` |
| `mmrisk_http_request_duration_seconds{endpoint}` | histogram | Time to answer |
| `mmrisk_transactions_scored_total{action}` | counter | First-time scores by ALLOW / REVIEW / BLOCK |
| `mmrisk_duplicate_requests_total` | counter | Requests for an id already scored |
| `mmrisk_risk_probability` | histogram | Calibrated probability of scored transactions |
| `mmrisk_service_ready` | gauge | 1 when a model is loaded |
| `mmrisk_history_transactions`, `mmrisk_customers` | gauge | Size of the in-memory state |
| `mmrisk_model_info{feature_version}` | gauge | 1 for the loaded feature version |

Alerts (`infrastructure/monitoring/alerts.yml`), all assumed: API down (1 min); no model (1 min); server errors above 5% (5 min); invalid scoring requests above 20% (10 min); p95 scoring time above 250 ms (10 min); flagged share above 8% over an hour with at least 200 scored (30 min); no flags in 6 hours with at least 500 scored (30 min). They show in Prometheus and Grafana; nothing is sent anywhere.

## 2. Do the decisions look right? (prediction store)
```powershell
.\scripts\dev.ps1 simulate       # fills data\predictions
.\scripts\dev.ps1 baseline       # freeze this store as the reference (only if you judge it healthy)
.\scripts\dev.ps1 simulate       # (new --store, or delete the folder) a later run
.\scripts\dev.ps1 monitor        # checks against the baseline if one exists
```
Report: `data\monitoring\report.md` and `report.json`. Exit code 1 only when a check FAILS.

| Check | PASS means | Result when it is not |
|---|---|---|
| unique transaction ids; probabilities in [0, 1]; action and level agree; no missing times; no reasons on ALLOW; reasons readable | the store is internally consistent | FAIL (a bug or corruption) |
| flagged transactions have reasons; one feature version | as expected | WARN |
| no empty windows | events in every window | WARN |
| store is fresh | newest prediction is within 6 h of `--as-of` | WARN; skipped without `--as-of` |
| review queue within capacity | peak REVIEW per hour is at most `--review-capacity-per-hour` | WARN; skipped without it |
| flagged / blocked share like the baseline | z score below 3 (WARN from 3, FAIL from 5) | skipped when under 5 flags are expected |
| volume like the baseline | within a factor of 2 of the baseline's events per hour | WARN |
| no unusual window | no testable window at |z| 5 or more | WARN only, never FAIL |

Reading a result: the shares are compared with a binomial z score, but fraud comes in clusters, so a z of 2 to 4 happens in healthy data (measured). A FAIL on the flagged share means decisions changed a lot (a different model, cost setting or input problem); it does not say which. These checks cannot tell whether decisions were right (labels arrive up to 45 days later) or whether inputs drifted (Phase 15).
