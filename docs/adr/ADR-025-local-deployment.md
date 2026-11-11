# ADR-025: Production-like local deployment
Status: accepted (Phase 20). Runbook: [deployment.md](../deployment.md).

## Context
Phases 12 and 18 gave a development compose file (open API on loopback) and the security features (keys, TLS, audit, refusal to bind outside without them). Nothing yet put them together as one way of running the service, and nothing checked the setup before start or the running service from outside.

## Decisions
- A second compose file, `docker-compose.prod.yml`, next to the development one. The development file stays simple; the production-like one cannot be started by accident with open settings because `preflight` rejects them.
- Secrets are files under `secrets\` mounted as Docker secrets (`/run/secrets/...`), never environment variables or image layers. The folder is git-ignored and docker-ignored. `init` prints keys once and stores only hashes (the same key store as Phase 18); the metrics token is the one exception (a file Prometheus must read) and is a separate, metrics-only role.
- One self-signed certificate (EC P-256, SANs `localhost`, `api`, `127.0.0.1`, `::1`) used as its own trust anchor. Clients pass `--cafile`; they do not disable verification. The `cryptography` package makes it (an optional `deploy` extra) so no `openssl` binary is needed on Windows.
- Containers: read-only filesystem with a tmpfs `/tmp`, all capabilities dropped, no-new-privileges, non-root, memory/CPU/PID limits, restart `unless-stopped`, rotating json logs, ports on 127.0.0.1 only. The limits are ASSUMED.
- The preflight reads the compose file as data and enforces those rules, instead of trusting that the file was written correctly. The same rules run in CI (`docker compose config`, plus the rule check) so a later edit that weakens them fails.
- `verify` tests the running stack from outside, as a client: boundaries (no key, wrong key, wrong role, plain HTTP) as well as the happy path. A service that answers 200 to everything would pass a smoke test and fail `verify`.
- Backup and restore are small and strict: an allow-list of folders, a manifest of SHA-256 hashes, everything verified before anything is written, refusal of links and unsafe names, no overwrite without `--force`, secrets only on request. It uses the standard library `tarfile` and does not call `tar`.
- Retraining stays on the host. Putting it in a container needs the training libraries and a writable registry, which would weaken the read-only mounts, for no gain on one machine.
- The API loads the production model at start; promotion or rollback is followed by a restart. A hot reload would add a second code path to the scoring service for a rare event.
- The client side was completed: the smoke test and the stream simulator take a key (from `MMRISK_API_KEY`) and a certificate file, so they work against the hardened service.

## Rejected
- Compose `profiles` or overrides to switch hardening on and off in one file: easy to run the wrong combination.
- Docker Swarm or Kubernetes secrets, a reverse proxy, Let's Encrypt: not meaningful on one machine without a domain; revisit in the cloud design (Phase 21).
- Disabling certificate verification in the clients to make a self-signed certificate work.

## Consequences
Tests: 78 unit (secrets, preflight rules with mutated compose files, verify logic, backup and restore security cases, CLI) and 13 integration against a real API process started with the compose command line (TLS and keys on, a registry model): verify passes, fails with a wrong or swapped key, fails without the trust anchor; smoke and the stream scorer with key and certificate; preflight on the same files; backup restored to a working registry. 17 mutations to the deploy logic were tried; 3 survived at first and tests were added (a changed member of the same size, `..` in a name, a 200 without metrics); one mutation (the chmod on the key file) is equivalent because the key store already writes it with mode 600.

**Not verified:** Docker was not available, so the images and the compose file were never run; the container health check, file permissions of secrets for user 10001 on Linux, Prometheus scraping over TLS with a bearer file, and Grafana's `__FILE` password are TO VERIFY. The runbook lists them.

**Update 2026-11-11.** CI later ran `docker compose config` and the file's own preflight rules for this compose file (green in run 42930127418), and the author ran the API container on Docker Desktop (`deploy verify` 12 of 12). The dashboard container crashed on first run (missing scikit-learn, fixed). The statement above is kept as written at the time. See [verification-status.md](../verification-status.md).
