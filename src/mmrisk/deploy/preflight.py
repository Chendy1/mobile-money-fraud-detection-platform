"""Checks run before `docker compose up`: refuse to start on a setup that is wrong or unsafe.

Everything here reads files and asks the operating system; nothing starts a container. Where a
check cannot be done (no Docker, no pyyaml, no cryptography) it says so as a warning rather than
passing silently.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import ssl
import subprocess
from pathlib import Path

from mmrisk.deploy import secrets_init as S
from mmrisk.deploy.checks import Check, fail, ok, warn
from mmrisk.security import passwords
from mmrisk.security.keys import KeyFileError, KeyStore

SECRET_WORDS = re.compile(r"(PASSWORD|SECRET|TOKEN|API_?KEY|CREDENTIAL)", re.I)
MIN_FREE_BYTES = 2 * 1024**3
CERT_WARN_DAYS = 14
PORTS = {8443: True, 8501: True, 9090: False, 3000: False}  # port: needed for the default start
MIN_PASSWORD = 12


def _mode_warning(path: Path) -> str | None:
    if os.name == "nt":
        return None
    mode = path.stat().st_mode & 0o777
    return f"mode {mode:o}" if mode & 0o077 else None


def check_secrets(root: Path) -> list[Check]:
    root, out = Path(root), []
    missing = [f for f in S.FILES if not (root / f).is_file() or (root / f).stat().st_size == 0]
    if missing:
        return [fail("secrets present", f"missing or empty in {root}: {', '.join(missing)}; "
                     "run `python -m mmrisk.deploy init`")]  # fmt: skip
    out.append(ok("secrets present", f"{len(S.FILES)} files in {root}"))
    loose = [f for f in S.FILES if f != S.CERT and _mode_warning(root / f)]
    if loose:
        out.append(warn("secret file permissions", f"readable by others: {', '.join(loose)}"))
    # the key file: all three roles active, and the stored metrics key is one of them
    try:
        store = KeyStore(root / S.API_KEYS)
        active = {k["role"] for k in store.keys() if not k["revoked_at"]}
        lacking = {"score", "metrics", "admin"} - active
        out.append(
            fail("API keys", f"no active key for: {', '.join(sorted(lacking))}")
            if lacking else ok("API keys", "an active key for each role")
        )  # fmt: skip
        token = (root / S.METRICS_KEY).read_text(encoding="utf-8").strip()
        key = store.check(token)
        if key and key["role"] == "metrics":
            out.append(ok("metrics key file matches an active metrics key"))
        else:
            out.append(
                fail(
                    "metrics key file",
                    "does not match an active key with the metrics role (Prometheus would get "
                    "401); re-run init or write a current key",
                )
            )
    except KeyFileError as exc:
        out.append(fail("API keys", str(exc)))
    out += _check_certificate(root)
    try:
        passwords.parse((root / S.DASH_HASH).read_text(encoding="utf-8").strip())
        out.append(ok("dashboard password hash"))
    except passwords.PasswordError as exc:
        out.append(fail("dashboard password hash", str(exc)))
    pw = (root / S.GRAFANA).read_text(encoding="utf-8").strip()
    out.append(
        ok("Grafana password") if len(pw) >= MIN_PASSWORD
        else fail("Grafana password", f"shorter than {MIN_PASSWORD} characters")
    )  # fmt: skip
    return out


def _check_certificate(root: Path) -> list[Check]:
    cert, key = root / S.CERT, root / S.KEY
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert), str(key))
    except (ssl.SSLError, OSError) as exc:
        return [fail("TLS certificate and key", f"do not load together: {exc}")]
    out = [ok("TLS certificate and key load together")]
    try:
        import datetime as dt

        from cryptography import x509
    except ImportError:
        return [*out, warn("TLS certificate expiry and names",
                           'not checked: pip install -e ".[deploy]"')]  # fmt: skip
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    left = (parsed.not_valid_after_utc - dt.datetime.now(dt.UTC)).days
    if left < 0:
        out.append(fail("TLS certificate expiry", f"expired {-left} days ago"))
    elif left < CERT_WARN_DAYS:
        out.append(warn("TLS certificate expiry", f"{left} days left"))
    else:
        out.append(ok("TLS certificate expiry", f"{left} days left"))
    try:
        san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = set(san.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        names = set()
    lacking = [n for n in S.CERT_NAMES if n not in names]
    out.append(
        warn("TLS certificate names", f"does not name: {', '.join(lacking)}")
        if lacking else ok("TLS certificate names", ", ".join(S.CERT_NAMES))
    )  # fmt: skip
    return out


def check_compose(path: Path) -> list[Check]:
    """Structural rules for the production compose file (no Docker needed)."""
    path = Path(path)
    try:
        import yaml
    except ImportError:
        return [warn("compose file rules", "not checked: pip install pyyaml")]
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        services = doc["services"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        return [fail("compose file", f"cannot read {path}: {exc}")]
    problems: list[str] = []
    for name, svc in services.items():
        if svc.get("read_only") is not True:
            problems.append(f"{name}: read_only is not true")
        if svc.get("cap_drop") != ["ALL"]:
            problems.append(f"{name}: cap_drop is not [ALL]")
        if "no-new-privileges:true" not in svc.get("security_opt", []):
            problems.append(f"{name}: no-new-privileges missing")
        if not (svc.get("mem_limit") and svc.get("cpus") and svc.get("pids_limit")):
            problems.append(f"{name}: a resource limit is missing")
        if svc.get("restart") not in ("unless-stopped", "on-failure", "always"):
            problems.append(f"{name}: no restart policy")
        for bad in ("privileged", "network_mode", "pid", "ipc", "cap_add", "devices"):
            if bad in svc:
                problems.append(f"{name}: {bad} is set")
        for mapping in svc.get("ports", []):
            if not str(mapping).startswith("127.0.0.1:"):
                problems.append(f"{name}: port {mapping} is not published on 127.0.0.1")
        for vol in svc.get("volumes", []):
            if "docker.sock" in str(vol):
                problems.append(f"{name}: mounts the Docker socket")
        image = svc.get("image", "")
        if image and (":" not in image or image.endswith(":latest")):
            problems.append(f"{name}: image {image} is not a specific tag")
        env = svc.get("environment") or {}
        items = env.items() if isinstance(env, dict) else [e.partition("=")[::2] for e in env]
        for key, value in items:
            if SECRET_WORDS.search(key) and not key.endswith("__FILE") and value:
                problems.append(f"{name}: {key} holds a secret in the compose file")
    declared = doc.get("secrets") or {}
    for sname, spec in declared.items():
        if not spec.get("file"):
            problems.append(f"secret {sname} has no file")
        elif "secrets" not in Path(spec["file"]).parts:
            problems.append(f"secret {sname} is not under the secrets folder")
    for name, svc in services.items():
        for used in svc.get("secrets", []):
            if used not in declared:
                problems.append(f"{name}: uses undeclared secret {used}")
    api = services.get("api", {})
    out = [fail("compose file rules", "; ".join(problems)) if problems
           else ok("compose file rules", f"{len(services)} services")]  # fmt: skip
    out.append(_check_api_command(api))
    return out


def _check_api_command(api: dict) -> Check:
    from mmrisk.api.cli import build_parser

    command = api.get("command") or []
    try:
        parsed = build_parser().parse_args(command[3:])
    except SystemExit:
        return fail("API command", "does not parse with `python -m mmrisk.api`")
    missing = [n for n, v in (("--auth-keys", parsed.auth_keys), ("--tls-cert", parsed.tls_cert),
                              ("--tls-key", parsed.tls_key)) if not v]  # fmt: skip
    if missing or parsed.allow_insecure_external:
        return fail("API command", "needs keys and TLS: " + (", ".join(missing) or
                    "--allow-insecure-external must not be used"))  # fmt: skip
    if not api.get("healthcheck"):
        return fail("API command", "no healthcheck")
    return ok("API command", "keys and TLS on, a healthcheck, parses")


def check_model(model_dir: Path) -> Check:
    try:
        from mmrisk.api.service import load_model

        model = load_model(Path(model_dir))
    except Exception as exc:  # a missing package, a missing file, a refused pickle: all stop us
        return fail("model", f"{model_dir}: {exc}")
    return ok("model loads safely", f"{model_dir}, feature version {model.feature_version}")


def check_lake(lake: Path) -> Check:
    try:
        from mmrisk.api.loading import read_lake

        txns, customers = read_lake(Path(lake))
    except Exception as exc:
        return fail("data lake", f"{lake}: {exc}")
    return ok("data lake", f"{txns.height} transactions, {customers.height} customers")


def check_ports(ports: dict[int, bool] | None = None) -> list[Check]:
    out = []
    for port, needed in (PORTS if ports is None else ports).items():
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                out.append(ok(f"port {port} free"))
            except OSError:
                make = fail if needed else warn
                out.append(make(f"port {port} free", "in use (stop what uses it, or skip with "
                                "--running when the stack is already up)"))  # fmt: skip
    return out


def _covers(lines: list[str], name: str) -> bool:
    return any(x.strip().strip("/") in (name, f"{name}/*", f"{name}/**") for x in lines)


def check_ignores(root: Path) -> list[Check]:
    root, out = Path(root), []
    for fname, needs in ((".gitignore", ("secrets", "backups")), (".dockerignore", ("secrets",))):
        try:
            lines = (root / fname).read_text(encoding="utf-8").splitlines()
        except OSError:
            out.append(fail(fname, "missing"))
            continue
        lacking = [n for n in needs if not _covers(lines, n)]
        out.append(fail(fname, f"does not ignore: {', '.join(lacking)}") if lacking
                   else ok(fname, "ignores " + ", ".join(needs)))  # fmt: skip
    return out


def check_docker(compose: Path) -> Check:
    if shutil.which("docker") is None:
        return warn("docker compose config", "Docker is not installed here: the file was only "
                    "checked structurally")  # fmt: skip
    try:
        done = subprocess.run(["docker", "compose", "-f", str(compose), "config", "-q"],
                              capture_output=True, text=True, timeout=60)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        return warn("docker compose config", f"could not run: {exc}")
    if done.returncode == 0:
        return ok("docker compose config", "the file is valid")
    return fail("docker compose config", done.stderr.strip()[-300:])


def check_disk(root: Path) -> Check:
    free = shutil.disk_usage(root).free
    gb = free / 1024**3
    return (
        ok("free disk", f"{gb:.1f} GB")
        if free >= MIN_FREE_BYTES
        else warn("free disk", f"{gb:.1f} GB (under {MIN_FREE_BYTES // 1024**3} GB)")
    )


def run_all(
    root: Path, secrets_dir: Path, compose: Path, model_dir: Path, lake: Path, running: bool = False
) -> list[Check]:
    checks = [*check_secrets(secrets_dir), *check_compose(compose), *check_ignores(root)]
    checks += [check_model(model_dir), check_lake(lake)]
    if not running:
        checks += check_ports()
    checks += [check_docker(compose), check_disk(root)]
    return checks
