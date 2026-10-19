# Threat model (local scoring API, Phase 11)
Scope: the API in `mmrisk.api` running on a developer's own machine with synthetic data. This is a starting list, extended in Phase 18 (security hardening). It is not a security audit.

| Threat | What exists now | What does not |
|---|---|---|
| Anyone on the network calls the API | Listens on 127.0.0.1 by default; another address needs `--allow-external` and prints a warning | No authentication, no TLS, no rate limit (Phase 18) |
| Malformed or hostile input | Strict schema (unknown fields, types, allowed values, limits, id pattern), 8 KB body limit, 422 errors that do not echo input | No request rate limit; no check that the customer and device ids are genuine |
| Replay or double submission | Same id and content returns the first answer once; same id with other content is refused | No signature on requests |
| Information leakage | Generic 500 body; request id for the logs; transaction bodies are not logged; the model description exposes only the feature version, the scorer name and the assumed costs | Access logs print the path and status; `/docs` shows the schema unless `--no-docs` |
| Poisoned model file | The loader checks the feature version | The bundle is a pickle: loading a file someone else made can run their code. Only load bundles this project created. Signing or a safe format is future work |
| Probing the model by repeated queries | Nothing | The score, the costs and the reason codes together let a caller learn the model; acceptable for synthetic data only |
| Denial of service by large history or many requests | One lock serialises scoring; latency per request is reported in `X-Process-Time-Ms` | Unbounded history per customer; no queue limit; load behaviour is Phase 19 |
| Misuse of the output | Costs are labelled ASSUMED; reason codes describe the model, not a cause | The API must not be used for real decisions about real people |
