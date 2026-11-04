# ADR-023: Security hardening
Status: accepted (Phase 18). Details and usage: [security.md](../security.md).

## Context
Phases 11 to 17 left the API and dashboard open to anyone who can reach them, with `--allow-external` as the only guard. The data is synthetic, but the project is meant to show habits that carry over.

## Decisions
- Keys, not users, for the API. Random `mmk_<id>_<secret>` tokens, SHA-256 hashes in a JSON file (a fast hash is acceptable for 256-bit random secrets; passwords chosen by people use scrypt). Three roles mapped to routes in one table. Constant-time compare, and a dummy hash is compared for unknown ids.
- One ASGI middleware, placed before the metrics middleware, so refusals are counted and nothing reaches the app unauthenticated. Order: open paths, failure block, key, role, rate limit. Unknown routes need a key too.
- Rate limits and failure blocks in memory, bounded in size. Simple and testable; lost on restart and per process (a limit when several workers run is not shared).
- External binding needs keys, TLS and a flag; the old flag alone is no longer enough. `--allow-insecure-external` exists for the container, where the host-side port mapping is the guard. Refusal is checked before any file is written.
- The audit log never receives bodies or identifiers; the logger rejects unknown field names, so a typo cannot add one.
- Allow-list unpickling of the model bundle. Chosen over a new file format because the format change would touch Phases 9 to 16; rejected as a sandbox claim. Fails closed.
- Dashboard: one password with scrypt, a global lock, because Streamlit gives no client address.
- Scanner is our own (regexes, no entropy) so it runs anywhere with no install; a dedicated tool is the better choice and is listed as TO VERIFY. Dependency audit is reported but does not fail CI because the findings are in transitive packages.
- All limits and counts (600/min, burst 50, 10 failures per 60 s, 5 per 300 s) are ASSUMED.

## Consequences
The Docker image and compose files changed one flag. Existing automation that called the API with `--allow-external` on a non-loopback host must now add keys and TLS or the insecure flag. Tests: 50 new (47 security, 2 login page, 1 launcher), 12 mutations each caught.
