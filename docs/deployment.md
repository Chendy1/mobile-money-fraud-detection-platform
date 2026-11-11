# Production-like local deployment (Phase 20)

One command set that runs the API and the dashboard the way a small production service would be run: keys, TLS, secrets in files, hardened containers that restart, health checks, rotating logs, resource limits, a preflight before start, a check from outside after start, and a backup you can restore. Design and limits: [ADR-025](adr/ADR-025-local-deployment.md). Threats: [threat-model.md](threat-model.md).

**What this is not.** It runs on one computer. It is not high availability, not a cloud deployment and not a security audit. The data is synthetic. A self-signed certificate protects the connection on this machine only. **Docker was not available where this phase was built.** The first real run was on the developer's machine and found one defect (the dashboard image), now fixed. Items not yet run are marked TO VERIFY.

## What was verified and what was not
| Item | Status |
|---|---|
| Secret creation, preflight rules, verify checks, backup and restore | Tested (unit tests and a real API process started with the compose file's command line, TLS and keys on). In the assistant's Linux sandbox. |
| The compose file as a file (rules: read-only, no capabilities, loopback ports, limits, secrets as files, no literal secrets) | Tested as a file; `docker compose config` run only in CI (TO VERIFY first result) |
| `docker compose up --build`, API image and its TLS health check | **Verified on the developer's Windows machine (Docker Desktop):** built, healthy, `deploy verify` 12 of 12 passed against the container |
| Dashboard image | First run crashed (`No module named 'joblib'`: the registry reader needs scikit-learn). Fixed in the image (extra `dashboard-image`) and guarded by a test; the rebuilt container is TO VERIFY |
| Reading `/app/data/audit` and secret files as user 10001 on Linux hosts | TO VERIFY (see "Permissions") |
| Prometheus scraping `https://api:8443/metrics` with the metrics key; Grafana with a password file | TO VERIFY |
| Windows behaviour of the `deploy` commands (init, preflight, verify, backup, restore) | TO VERIFY (the unit tests pass in the sandbox only) |

## 1. Prerequisites
- A trained model in the registry (`models\registry`, Phase 16: `dev.ps1 regboot`, or a retraining run) and the processed lake (`data\processed`, Phase 3).
- Docker Desktop running (WSL2). Stop other containers first; the stack is limited to about 1.4 GB of memory plus the optional monitoring (0.75 GB).
- The package with the `deploy` extra: `.\.venv\Scripts\python -m pip install -e ".[deploy]"` (adds `cryptography` and `pyyaml`, plus the same libraries the API image installs: `preflight` loads the model and `verify` imports the API package).

## 2. Make the secrets (once)
```powershell
cd C:\Users\geek\projects\mobile-money-fraud-detection-platform
.\.venv\Scripts\python -m mmrisk.deploy init
```
Expected: it prints a score key, an admin key and a dashboard password **once**, and the certificate's expiry date. Copy them to a password manager now; only hashes are stored.

Creates `secrets\` with: `api_keys.json` (hashed keys: score, metrics, admin), `metrics_key` (the metrics token Prometheus uses), `tls\cert.pem` and `tls\key.pem` (a self-signed certificate for `localhost`, `api`, `127.0.0.1` and `::1`, valid 365 days, used as its own trust anchor), `dashboard.hash` (scrypt hash of the dashboard password) and `grafana_admin_password`. `secrets\` and `backups\` are git-ignored and docker-ignored, and the secret scanner refuses them if staged. Running `init` again is refused unless you pass `--force`, which replaces every key.

## 3. Preflight
```powershell
.\.venv\Scripts\python -m mmrisk.deploy preflight
```
Checks, each printed as PASS, WARN or FAIL (exit code 1 on any FAIL; `--strict` also fails on WARN):
- secrets present, not empty, hashes valid, the metrics key belongs to an active metrics-role key, the certificate loads, matches its key, is not expired (WARN under 14 days) and names `localhost`, `api` and `127.0.0.1`;
- the compose file: every service read-only with all capabilities dropped and `no-new-privileges`, memory/CPU/process limits, a restart policy, no privileged mode or host networking, no Docker socket, ports bound to `127.0.0.1` only, pinned image tags, no literal secret in an environment variable, secrets only from `secrets\` and declared; the API command has keys, TLS and no `--allow-insecure-external`, and a health check;
- the model registry loads through the safe loader; the lake exists; `.gitignore` and `.dockerignore` hold the secrets and backups rules;
- the ports 8443 and 8501 are free (9090 and 3000 only WARN), Docker answers, disk space.

Fix every FAIL before going on. Run it with `--running` when the stack is already up (it then skips the port check).

## 4. Start
```powershell
docker compose -f docker-compose.prod.yml up --build -d
docker compose -f docker-compose.prod.yml ps        # wait for "healthy" on api (the history replay takes a minute or two)
```
`dev.ps1 up-prod` is the same. Optional monitoring: add `--profile monitoring` (Prometheus on 127.0.0.1:9090 scrapes the API over TLS with the metrics key; Grafana on 127.0.0.1:3000, admin password from `secrets\grafana_admin_password`). Every published port is on 127.0.0.1; nothing is reachable from the network.

Services: `api` (8443, TLS, keys, audit log), `dashboard` (8501, password login, read-only mounts). Retraining stays on the host on purpose: it needs the training libraries and writes the registry, and the containers mount the registry read-only.

## 5. Verify from outside
```powershell
$env:MMRISK_API_KEY = "<the score key printed by init>"
.\.venv\Scripts\python -m mmrisk.deploy verify
```
It uses `https://127.0.0.1:8443`, trusts only `secrets\tls\cert.pem` and reads the metrics key from `secrets\metrics_key`. It runs the Phase 11 smoke test with the key and then checks the boundaries: no key is refused (also `/docs`), a wrong key is refused, a score key cannot read `/metrics`, the metrics key can read it and cannot score or describe the model, and plain HTTP to the TLS port gets no answer. Exit 1 on any failure.

The simulator and the smoke test accept the same key and certificate: `python -m mmrisk.api smoke --url https://127.0.0.1:8443 --cafile secrets\tls\cert.pem` and `python -m mmrisk.streaming run --url ... --cafile ...` with `MMRISK_API_KEY` set (the key is read from the environment, never an argument).

Dashboard: http://127.0.0.1:8501 with the password from `init`. It is plain HTTP on loopback; TLS in front of it is not part of this phase.

## 6. Daily operation
```powershell
docker compose -f docker-compose.prod.yml logs --tail 100 api
docker compose -f docker-compose.prod.yml ps
Get-Content data\audit\audit.jsonl -Tail 20
```
Containers restart on failure (`unless-stopped`) and after Docker restarts. Logs rotate at 10 MB x 3 files per container. The audit log is a file on the host (`data\audit`); it rotates itself (Phase 18).

**Promote or roll back a model.** The API loads the production version once, at start. After `python -m mmrisk.registry promote ...`, `rollback`, or a retraining run that promotes (Phase 16), restart it: `docker compose -f docker-compose.prod.yml restart api`, then run `verify`. It always serves what `production.json` names; it does not reload by itself.

**Rotate a key.** `python -m mmrisk.security keys --file secrets\api_keys.json create --name app2 --role score` (prints the key once) and `... revoke <id>`; a revoked key stops working on the next request (tested in Phase 18). If you replace the metrics key, write the new token into `secrets\metrics_key` and restart Prometheus; `preflight` fails when that file names a revoked or non-metrics key.

**Renew the certificate.** `preflight` warns 14 days before expiry. Delete `secrets\tls`, run `init --force` only if you also accept new keys, or generate a new certificate with `python -c "from pathlib import Path; from mmrisk.deploy.secrets_init import make_certificate as m; m(Path('secrets/tls/cert.pem'), Path('secrets/tls/key.pem'))"`, then restart the stack.

## 7. Backup and restore
```powershell
.\.venv\Scripts\python -m mmrisk.deploy backup --out backups\mmrisk-1.tgz        # dev.ps1 backup adds a timestamp
.\.venv\Scripts\python -m mmrisk.deploy restore backups\mmrisk-1.tgz --target C:\restore-test --dry-run
.\.venv\Scripts\python -m mmrisk.deploy restore backups\mmrisk-1.tgz --target C:\restore-test
```
A backup holds the model registry, `data\retrain`, `data\drift`, `data\monitoring` and `data\audit` (and `data\predictions` with `--with-predictions`). It never includes secrets unless you pass `--include-secrets`, and a restore skips them unless you pass it again. The archive carries `MANIFEST.json` with the SHA-256 and size of every file. A restore reads the whole archive and checks every hash **before writing anything**; it refuses links, absolute or `..` paths, backslashes, folders outside the allow-list, a manifest that disagrees with the members, and (without `--force`) any file that already exists. Files are written to a temporary name and renamed. Test a restore into an empty folder from time to time; a backup you have not restored is a hope.

The lake (`data\processed`) is not backed up: it is rebuilt from the generator and the ingestion step (`dev.ps1 generate`, `dev.ps1 ingest`).

## 8. Stop and remove
```powershell
docker compose -f docker-compose.prod.yml --profile monitoring down      # dev.ps1 down-prod
docker compose -f docker-compose.prod.yml --profile monitoring down -v   # also deletes Prometheus and Grafana data
```
To remove the secrets, delete the `secrets\` folder yourself (it is not touched by `down`).

## Permissions (Linux and WSL hosts) - TO VERIFY
The images run as user 10001. Docker file secrets are mounted read-only and keep the host file's owner and mode; `init` makes them mode 600 owned by you, so a container user other than you may not be able to read them on a Linux host. Docker Desktop on Windows usually presents them as readable. If the API log says permission denied for `/run/secrets/...`, either `chmod 644` the files you accept to expose to local users or set the files' group to 10001 and mode 640. The same applies to `data\audit` (the API must write there: `chown 10001` or `chmod 777` on a private machine) and to Prometheus (user nobody) reading `metrics_key`. The assistant could not test any of this.

## Known limits
- One machine, one API process: no failover, no rolling update; a restart drops in-memory customer history and replays it from the lake.
- The self-signed certificate is trusted only because the clients are told to trust that one file. Browsers will warn for the dashboard if you put TLS there yourself.
- Image tags are pinned by tag, not by digest; no image scanning is run (TO VERIFY / future work); no Alertmanager.
- The API trusts the registry folder it is given: anyone who can write there can change the model (the loader refuses code in model files but is not a sandbox).
- Resource limits (1 GB, 1 CPU for the API) are ASSUMED, not tuned; the Phase 19 numbers were measured without them.
