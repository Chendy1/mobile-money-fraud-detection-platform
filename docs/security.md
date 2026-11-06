# Security guide (Phase 18)
What was added, how to use it, and what it does not do. Design: [ADR-023](adr/ADR-023-security-hardening.md). Threats: [threat-model.md](threat-model.md). Synthetic data only; this is a hardening exercise for a portfolio project, not a security audit and not a certification.

## API keys
```powershell
.\.venv\Scripts\python -m mmrisk.security keys create --name ops --role score   # prints the key once
.\.venv\Scripts\python -m mmrisk.security keys list
.\.venv\Scripts\python -m mmrisk.security keys revoke <id>
```
Keys live in `data\security\api_keys.json` as SHA-256 hashes (the file is git-ignored by name and refused by the scanner if it is ever staged). A key is `mmk_<id>_<secret>`; the id is public and appears in the audit log. Roles: `score` (POST /v1/score, GET /v1/model), `metrics` (GET /metrics), `admin` (both). `/health` and `/ready` need no key.

Start the API with keys: `python -m mmrisk.api serve --auth-keys data\security\api_keys.json`. Send `Authorization: Bearer <key>` (never put a key in the URL). With keys on, `/docs` is off (a browser cannot send the header). Unknown paths also answer 401, so the route list cannot be probed.

Limits (all ASSUMED, not tuned): 600 requests a minute per key with a burst of 50 (`--rate-per-minute`, `--rate-burst`); 10 failed keys from one address in 60 s blocks that address for the rest of the window (429 with `Retry-After`). State is in memory and per process.

## Audit log
`data\security\audit.jsonl`, one JSON object per line: time, event (`startup`, `auth_failed`, `forbidden`, `blocked`, `rate_limited`, `key_created`, `key_revoked`), key id, role, method, route, status, client address, request id. Never keys, request bodies, customer or transaction ids. Successful scoring calls are not logged (they are in the metrics). Rotates once at 5 MB. It is a local file: whoever can write it can change it. Read it with `python -m mmrisk.security audit --last 20`.

## Listening on a network address
The default is 127.0.0.1. For any other `--host` the API now refuses to start unless all three are given: `--auth-keys`, `--tls-cert` with `--tls-key`, and `--allow-external`. `--allow-insecure-external` removes all of it (the Docker image uses it because the container's own interface is not the published one; compose publishes the port on 127.0.0.1 only).

Try TLS locally with a self-signed certificate (a browser will warn; do not use one in front of real users):
```powershell
openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=localhost" -keyout data\security\key.pem -out data\security\cert.pem
```
`*.pem` and `*.key` are ignored by Git and Docker, and flagged by the scanner. TLS here is uvicorn's own; certificates, renewal and a reverse proxy are not handled.

## Dashboard login
```powershell
.\.venv\Scripts\python -m mmrisk.security dash-password       # asks twice; writes data\security\dashboard.hash
.\.venv\Scripts\python -m mmrisk.dashboard run --password-file data\security\dashboard.hash
```
One shared password (at least 12 characters), stored as an scrypt hash; the hash, not the password, is passed to the page in an environment variable. 5 wrong tries in 5 minutes lock every attempt for the rest of the window (Streamlit does not tell the script a client address, so the lock is global). A non-loopback `--host` needs `--password-file` and `--allow-external`. The dashboard has no TLS: use an SSH tunnel or a TLS proxy. Not checked: how the login looks in a browser (TO VERIFY).

## Model files
`RiskModel.load` and the API now read the model with an allow-list unpickler: a file that refers to `subprocess`, `os.system` and the like is refused with an error that names it (tested with a real malicious pickle). This is not a sandbox. Allowed classes run their own state-restoring code, and the check depends on joblib's internals (it fails closed if they change). Still only load bundles you made. `RiskModel.load(path, safe=False)` is the old behaviour. The registry already records SHA-256 for each version.

## Secret scan and dependency audit
`dev.ps1 secscan` (also run in CI and by a test) looks in tracked and untracked, non-ignored files for private keys, AWS and GitHub tokens, this project's own API keys, long literal `password=`-style values, and files that should not exist in the repo (`.env`, `*.pem`, `api_keys.json`, Terraform state). A line with `nosecret` is skipped. It has no entropy check and does not scan Git history; a secret committed once stays in history even if deleted (rotate it). Use a dedicated tool (for example gitleaks) for more: TO VERIFY, not run here.

`dev.ps1 audit` runs `pip-audit`. In the sandbox on 2026-11-04 it reported known vulnerabilities in `mlflow 3.17.1` (fix 3.17.2), `pillow 12.3.0` (39 advisories, fix 13.0.0, pulled in by matplotlib and streamlit) and `pyasn1 0.6.4` (fix 0.6.5). Those are the versions the sandbox resolved; your Windows environment may differ. Nothing was upgraded in this phase: the fixes were not tested against the project's tests. CI runs the audit as a non-blocking job. Dev-only use (tracking, plots, a local dashboard) lowers the exposure; it does not remove the finding.

## What is not done
No secret store (Grafana's password is still an environment variable), no key expiry, no per-user accounts or SSO, no TLS between containers, no WAF, no Alertmanager, no log shipping, no image scanning or digest pinning, GitHub Actions are pinned by tag not commit SHA (the SHAs were not verified offline: TO VERIFY), no penetration test, nothing run against a real network.

## Running it as a service (Phase 20)
[deployment.md](deployment.md) puts these features together: `python -m mmrisk.deploy init` makes the keys, certificate and passwords as files; `preflight` checks the setup; `verify` tests the running service from outside. The smoke test and the stream simulator send the key from `MMRISK_API_KEY` and trust the certificate with `--cafile`.
