# ruff: noqa: F811, F401
"""Phase 20 against a real API process started the way docker-compose.prod.yml starts it.

Same command line as the compose file (registry root, keys, TLS, audit log), with local paths in
place of /run/secrets and /app. Docker itself is not used here.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_api import COLUMNS, data, env, features, model, model_dir, ordered, wire  # noqa: F401

from mmrisk.api.cli import main as api_main  # noqa: E402
from mmrisk.deploy import backup as B  # noqa: E402
from mmrisk.deploy import preflight as P  # noqa: E402
from mmrisk.deploy import secrets_init as S  # noqa: E402
from mmrisk.deploy.cli import main as deploy_main  # noqa: E402
from mmrisk.deploy.verify import verify  # noqa: E402
from mmrisk.loadtest.cli import stop_process  # noqa: E402
from mmrisk.registry import Registry  # noqa: E402
from mmrisk.streaming import HttpScorer  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
BOOT_SECONDS = 90


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(url, cafile, proc, log):
    import ssl
    import urllib.request

    ctx = ssl.create_default_context(cafile=str(cafile)) if cafile else None
    end = time.monotonic() + BOOT_SECONDS
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise AssertionError(f"API exited ({proc.returncode}):\n{log.read_text()[-1500:]}")
        try:
            with urllib.request.urlopen(url + "/ready", timeout=2, context=ctx) as r:  # noqa: S310
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise AssertionError("API not ready")


def start(cmd, log):
    out = open(log, "w")  # noqa: SIM115
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)  # noqa: S603
    return proc, out


@pytest.fixture(scope="module")
def stack(tmp_path_factory, env, model_dir):
    root = tmp_path_factory.mktemp("prod")
    secrets = root / "secrets"
    created = S.init(secrets)
    registry = root / "models" / "registry"
    reg = Registry(registry)
    version = reg.register(model_dir, source="bootstrap", stage="approved")
    reg.promote(version, "integration test")
    audit = root / "data" / "audit"
    audit.mkdir(parents=True)
    port = free_port()
    cmd = [sys.executable, "-m", "mmrisk.api", "serve", "--host", "127.0.0.1", "--port", str(port),
           "--model-dir", str(registry), "--lake", str(env[1]), "--no-history",
           "--auth-keys", str(secrets / S.API_KEYS), "--tls-cert", str(secrets / S.CERT),
           "--tls-key", str(secrets / S.KEY), "--audit-log", str(audit / "audit.jsonl"),
           "--log-level", "warning"]  # fmt: skip
    log = root / "server.log"
    proc, out = start(cmd, log)
    url = f"https://127.0.0.1:{port}"
    try:
        wait_ready(url, secrets / S.CERT, proc, log)
        yield {"root": root, "secrets": secrets, "url": url, "created": created,
               "registry": registry, "version": version, "audit": audit / "audit.jsonl",
               "port": port}  # fmt: skip
    finally:
        stop_process(proc)
        out.close()


def metrics_key(stack):
    return (stack["secrets"] / S.METRICS_KEY).read_text().strip()


def run_verify(stack, score_key=None, metrics=None):
    return verify(
        stack["url"],
        stack["secrets"] / S.CERT,
        score_key or stack["created"].score_key,
        metrics or metrics_key(stack),
    )


def test_verify_passes_against_the_hardened_stack_and_checks_each_boundary(stack):
    results = run_verify(stack)
    assert [c for c in results if c.status != "pass"] == []
    names = " | ".join(c.name for c in results)
    for part in ("no key", "wrong key", "metrics", "plain HTTP"):
        assert part in names


def test_a_wrong_score_key_or_a_swapped_role_is_reported_as_a_failure(stack):
    wrong = run_verify(stack, score_key="mmk_00000000_" + "A" * 43)
    assert any(c.status == "fail" for c in wrong)
    swapped = run_verify(stack, score_key=stack["created"].admin_key)
    assert any(c.status == "fail" for c in swapped)  # an admin key is not a score-only key


def test_verify_without_the_certificate_as_trust_anchor_fails_tls(stack):
    results = verify(stack["url"], None, stack["created"].score_key, metrics_key(stack))
    assert any(c.status == "fail" for c in results)


def test_the_verify_command_exits_zero_and_prints_pass_lines(stack, monkeypatch, capsys):
    monkeypatch.setenv("MMRISK_API_KEY", stack["created"].score_key)
    code = deploy_main(["verify", "--url", stack["url"],
                        "--cafile", str(stack["secrets"] / S.CERT),
                        "--metrics-key-file", str(stack["secrets"] / S.METRICS_KEY)])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0 and "PASS" in out and "FAIL" not in out


def test_the_verify_command_exits_one_with_a_wrong_key(stack, monkeypatch, capsys):
    monkeypatch.setenv("MMRISK_API_KEY", "mmk_00000000_" + "B" * 43)
    code = deploy_main(["verify", "--url", stack["url"],
                        "--cafile", str(stack["secrets"] / S.CERT),
                        "--metrics-key-file", str(stack["secrets"] / S.METRICS_KEY)])  # fmt: skip
    assert code == 1 and "FAIL" in capsys.readouterr().out


def test_smoke_works_with_the_key_from_the_environment_and_the_certificate(
    stack, monkeypatch, capsys
):
    monkeypatch.setenv("MMRISK_API_KEY", stack["created"].score_key)
    code = api_main(["smoke", "--url", stack["url"], "--cafile", str(stack["secrets"] / S.CERT)])
    assert code == 0 and "FAIL" not in capsys.readouterr().out


def test_smoke_without_the_certificate_or_the_key_fails(stack, monkeypatch, capsys):
    monkeypatch.delenv("MMRISK_API_KEY", raising=False)
    assert api_main(["smoke", "--url", stack["url"]]) != 0
    monkeypatch.setenv("MMRISK_API_KEY", stack["created"].score_key)
    assert api_main(["smoke", "--url", stack["url"]]) != 0  # key but no trust anchor


def test_the_stream_scorer_uses_the_key_and_certificate_and_a_wrong_key_is_an_error(stack, ordered):
    txn = wire(ordered.to_dicts()[-1])
    cafile = str(stack["secrets"] / S.CERT)
    good = HttpScorer(stack["url"], key=stack["created"].score_key, cafile=cafile)
    assert good.ready()
    assert good.score(txn).status == "scored"
    bad = HttpScorer(stack["url"], key="mmk_00000000_" + "C" * 43, cafile=cafile)
    out = bad.score(txn)
    assert out.status == "error" and "401" in out.detail
    nocert = HttpScorer(stack["url"], key=stack["created"].score_key)
    assert nocert.ready() is False


def test_preflight_on_the_running_stack_files_has_no_failures(stack, env, capsys):
    results = P.run_all(
        REPO,
        stack["secrets"],
        REPO / "docker-compose.prod.yml",
        stack["registry"],
        env[1],
        running=True,
    )
    fails = [c for c in results if c.status == "fail"]
    # the repository's own .gitignore must hold the rules; the model and lake checks must pass
    assert [c for c in fails if c.name not in {"Docker available"}] == [], fails


def test_the_audit_log_records_the_calls_without_any_key_text(stack):
    text = stack["audit"].read_text()
    assert text.strip()
    assert stack["created"].score_key.split("_")[2] not in text
    assert metrics_key(stack).split("_")[2] not in text
    for line in text.splitlines():
        json.loads(line)


def test_the_backup_of_the_registry_restores_to_a_working_model(stack, tmp_path):
    arc = tmp_path / "backup.tgz"
    info = B.backup(stack["root"], arc)
    assert arc.exists() and info["files"] >= 1
    target = tmp_path / "restored"
    target.mkdir()
    B.restore(arc, target)
    restored = Registry(target / "models" / "registry")
    assert restored.production()["version"] == stack["version"]
    restored.verify(stack["version"])  # checksum still matches
    assert not (target / "secrets").exists()


def test_a_backup_never_holds_the_secrets_unless_asked(stack, tmp_path):
    arc = tmp_path / "plain.tgz"
    B.backup(stack["root"], arc)
    import tarfile

    with tarfile.open(arc) as t:
        names = t.getnames()
    assert not any("secrets" in n or "api_keys" in n or "key.pem" in n for n in names)


def test_the_server_refuses_to_bind_outside_without_the_hardening(stack, env, capsys):
    registry = str(stack["registry"])
    code = api_main(
        ["serve", "--host", "0.0.0.0", "--model-dir", registry, "--lake", str(env[1]),
         "--port", str(free_port())]
    )  # fmt: skip
    assert code != 0 and "refusing to listen" in capsys.readouterr().err
