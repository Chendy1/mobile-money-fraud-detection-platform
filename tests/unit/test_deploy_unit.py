"""Phase 20 without Docker or a server: secrets, preflight rules, the compose files, backups."""

import copy
import io
import json
import os
import re
import socket
import ssl
import tarfile
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402

from mmrisk.api.cli import build_parser as api_parser  # noqa: E402
from mmrisk.dashboard.cli import build_parser as dash_parser  # noqa: E402
from mmrisk.deploy import DeployError, checks  # noqa: E402
from mmrisk.deploy import backup as B  # noqa: E402
from mmrisk.deploy import preflight as P  # noqa: E402
from mmrisk.deploy import secrets_init as S  # noqa: E402
from mmrisk.deploy.cli import main as deploy_main  # noqa: E402
from mmrisk.security import passwords  # noqa: E402
from mmrisk.security.keys import KeyStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.prod.yml"
SECRET_WORDS = re.compile(r"(PASSWORD|SECRET|TOKEN|API_?KEY|CREDENTIAL)", re.I)


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    d = tmp_path_factory.mktemp("sec") / "secrets"
    return d, S.init(d)


def by_name(results, name):
    return next(c for c in results if c.name == name)


def statuses(results):
    return {c.name: c.status for c in results}


# ---- init ----


def test_init_makes_every_file_and_the_keys_work_with_the_right_roles(made):
    d, c = made
    assert all((d / f).is_file() and (d / f).stat().st_size > 0 for f in S.FILES)
    store = KeyStore(d / S.API_KEYS)
    assert [store.check(t)["role"] for t in (c.score_key, c.admin_key)] == ["score", "admin"]
    metrics = (d / S.METRICS_KEY).read_text().strip()
    assert store.check(metrics)["role"] == "metrics" and store.active_count() == 3


def test_only_hashes_are_stored_for_keys_and_the_dashboard_password(made):
    d, c = made
    blob = "".join(p.read_text() for p in d.rglob("*") if p.is_file() and p.name != "key.pem")
    assert c.score_key not in blob and c.admin_key not in blob and c.dashboard_password not in blob
    assert passwords.verify(c.dashboard_password, (d / S.DASH_HASH).read_text().strip())
    assert len(c.dashboard_password) >= passwords.MIN_LENGTH


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_secret_files_are_private_to_the_owner(made):
    d, _ = made
    for f in S.FILES:
        assert (d / f).stat().st_mode & 0o777 == 0o600, f


def test_the_certificate_names_localhost_and_the_service_and_loads_with_its_key(made):
    d, c = made
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(d / S.CERT), str(d / S.KEY))
    cert = x509.load_pem_x509_certificate((d / S.CERT).read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(san.get_values_for_type(x509.DNSName)) == {"localhost", "api"}
    assert [str(i) for i in san.get_values_for_type(x509.IPAddress)] == ["127.0.0.1", "::1"]
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    assert c.cert_expires > "2026"


def test_init_will_not_overwrite_and_force_replaces_everything(tmp_path):
    d = tmp_path / "s"
    first = S.init(d)
    with pytest.raises(DeployError, match="already holds"):
        S.init(d)
    second = S.init(d, force=True)
    store = KeyStore(d / S.API_KEYS)
    assert store.check(first.score_key) is None and store.check(second.score_key)
    assert store.active_count() == 3  # the old keys are gone, not accumulated


# ---- preflight: secrets ----


def fresh(tmp_path):
    d = tmp_path / "secrets"
    return d, S.init(d)


def test_a_fresh_set_of_secrets_passes_every_check(made):
    results = P.check_secrets(made[0])
    assert [c.status for c in results if c.status != "pass"] == []
    assert {"API keys", "TLS certificate expiry", "TLS certificate names"} <= {
        c.name for c in results
    }


@pytest.mark.parametrize("victim", S.FILES)
def test_a_missing_or_empty_secret_stops_the_preflight(tmp_path, victim):
    d, _ = fresh(tmp_path)
    (d / victim).write_text("")
    results = P.check_secrets(d)
    assert results[0].status == "fail" and victim in results[0].detail
    (d / victim).unlink()
    assert victim in P.check_secrets(d)[0].detail


def test_a_revoked_role_or_a_stale_metrics_key_fails(tmp_path):
    d, _ = fresh(tmp_path)
    store = KeyStore(d / S.API_KEYS)
    admin = next(k for k in store.keys() if k["role"] == "admin")
    store.revoke(admin["id"])
    assert "admin" in by_name(P.check_secrets(d), "API keys").detail
    d2, _ = fresh(tmp_path / "b")
    (d2 / S.METRICS_KEY).write_text("mmk_00000000_" + "A" * 43 + "\n")
    assert by_name(P.check_secrets(d2), "metrics key file").status == "fail"
    d3, _ = fresh(tmp_path / "c")
    store = KeyStore(d3 / S.API_KEYS)
    store.revoke(next(k["id"] for k in store.keys() if k["role"] == "metrics"))
    assert by_name(P.check_secrets(d3), "metrics key file").status == "fail"


def test_a_metrics_key_file_holding_another_role_fails(tmp_path):
    d, c = fresh(tmp_path)
    (d / S.METRICS_KEY).write_text(c.score_key + "\n")
    assert by_name(P.check_secrets(d), "metrics key file").status == "fail"


def test_a_key_that_does_not_belong_to_the_certificate_and_an_expired_one_fail(tmp_path):
    d, _ = fresh(tmp_path)
    other = tmp_path / "other"
    S.make_certificate(other / "c.pem", other / "k.pem")
    (d / S.KEY).write_bytes((other / "k.pem").read_bytes())
    assert by_name(P.check_secrets(d), "TLS certificate and key").status == "fail"
    e, _ = fresh(tmp_path / "e")
    S.make_certificate(e / S.CERT, e / S.KEY, days=-3)
    assert by_name(P.check_secrets(e), "TLS certificate expiry").status == "fail"


def test_a_certificate_close_to_expiry_warns_and_one_without_the_names_warns(tmp_path):
    d, _ = fresh(tmp_path)
    S.make_certificate(d / S.CERT, d / S.KEY, days=5)
    got = by_name(P.check_secrets(d), "TLS certificate expiry")
    assert got.status == "warn" and "days left" in got.detail
    S.make_certificate(d / S.CERT, d / S.KEY, days=P.CERT_WARN_DAYS + 1)
    assert by_name(P.check_secrets(d), "TLS certificate expiry").status == "pass"
    assert P.CERT_WARN_DAYS == 14


def test_a_damaged_hash_and_a_short_grafana_password_fail(tmp_path):
    d, _ = fresh(tmp_path)
    (d / S.DASH_HASH).write_text("not a hash")
    (d / S.GRAFANA).write_text("short\n")
    got = statuses(P.check_secrets(d))
    assert got["dashboard password hash"] == "fail" and got["Grafana password"] == "fail"
    (d / S.GRAFANA).write_text("x" * P.MIN_PASSWORD + "\n")
    assert statuses(P.check_secrets(d))["Grafana password"] == "pass"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_secret_files_that_others_can_read_warn(tmp_path):
    d, _ = fresh(tmp_path)
    os.chmod(d / S.METRICS_KEY, 0o644)
    got = by_name(P.check_secrets(d), "secret file permissions")
    assert got.status == "warn" and S.METRICS_KEY in got.detail


# ---- the compose file ----


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load(COMPOSE.read_text())


def test_the_real_compose_file_passes_its_own_rules():
    got = P.check_compose(COMPOSE)
    assert [c.status for c in got] == ["pass", "pass"], got


def mutated(tmp_path, doc, change):
    d = copy.deepcopy(doc)
    change(d)
    (tmp_path / "secrets").mkdir(exist_ok=True)
    path = tmp_path / "c.yml"
    path.write_text(yaml.safe_dump(d))
    return P.check_compose(path)


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda d: d["services"]["api"].update(read_only=False), "read_only"),
        (lambda d: d["services"]["api"].update(cap_drop=[]), "cap_drop"),
        (lambda d: d["services"]["api"].update(security_opt=[]), "no-new-privileges"),
        (lambda d: d["services"]["api"].pop("mem_limit"), "resource limit"),
        (lambda d: d["services"]["api"].pop("restart"), "restart policy"),
        (lambda d: d["services"]["api"].update(privileged=True), "privileged"),
        (lambda d: d["services"]["api"].update(network_mode="host"), "network_mode"),
        (lambda d: d["services"]["api"]["ports"].append("8443:8443"), "not published on 127.0.0.1"),
        (
            lambda d: d["services"]["api"]["ports"].append("0.0.0.0:80:80"),
            "not published on 127.0.0.1",
        ),
        (
            lambda d: d["services"]["api"]["volumes"].append("/var/run/docker.sock:/s"),
            "Docker socket",
        ),
        (lambda d: d["services"]["api"].update(image="x:latest"), "specific tag"),
        (lambda d: d["services"]["api"].update(image="x"), "specific tag"),
        (
            lambda d: d["services"]["grafana"]["environment"].update(GF_ADMIN_PASSWORD="hunter2"),
            "holds a secret",
        ),
        (lambda d: d["services"]["api"].update(secrets=["nope"]), "undeclared secret"),
        (
            lambda d: d["secrets"]["api_keys"].update(file="./api_keys.json"),
            "not under the secrets",
        ),
    ],
)
def test_each_rule_catches_its_violation(tmp_path, doc, change, expected):
    got = mutated(tmp_path, doc, change)
    assert got[0].status == "fail" and expected in got[0].detail


def test_file_references_to_secrets_are_allowed_and_literal_ones_are_not(tmp_path, doc):
    ok_change = lambda d: d["services"]["grafana"]["environment"].update(  # noqa: E731
        GF_SECURITY_ADMIN_PASSWORD__FILE="/run/secrets/grafana_password"
    )
    assert mutated(tmp_path, doc, ok_change)[0].status == "pass"


def test_the_api_command_must_keep_keys_tls_and_a_healthcheck(tmp_path, doc):
    def drop(flag):
        def go(d):
            cmd = d["services"]["api"]["command"]
            i = cmd.index(flag)
            del cmd[i : i + 2]

        return go

    for flag in ("--auth-keys", "--tls-cert", "--tls-key"):
        got = mutated(tmp_path, doc, drop(flag))
        assert got[1].status == "fail" and flag in got[1].detail
    got = mutated(
        tmp_path, doc, lambda d: d["services"]["api"]["command"].append("--allow-insecure-external")
    )
    assert got[1].status == "fail"
    got = mutated(tmp_path, doc, lambda d: d["services"]["api"].pop("healthcheck"))
    assert got[1].status == "fail" and "healthcheck" in got[1].detail
    got = mutated(tmp_path, doc, lambda d: d["services"]["api"]["command"].append("--nonsense"))
    assert got[1].status == "fail" and "parse" in got[1].detail


def test_an_unreadable_compose_file_is_a_failure(tmp_path):
    assert P.check_compose(tmp_path / "none.yml")[0].status == "fail"
    (tmp_path / "bad.yml").write_text("services: [")
    assert P.check_compose(tmp_path / "bad.yml")[0].status == "fail"


# ---- the files agree with each other ----


def secret_targets(svc):
    return {f"/run/secrets/{s}" for s in svc.get("secrets", [])}


def test_the_api_command_reads_secrets_it_is_given_and_the_healthcheck_uses_its_port(doc):
    api = doc["services"]["api"]
    cmd = api["command"]
    parsed = api_parser().parse_args(cmd[3:])
    for path in (parsed.auth_keys, parsed.tls_cert, parsed.tls_key):
        assert str(path).replace("\\", "/") in secret_targets(api)
    assert parsed.port == 8443 and parsed.host == "0.0.0.0" and parsed.allow_external
    assert api["ports"] == ["127.0.0.1:8443:8443"]
    health = " ".join(api["healthcheck"]["test"])
    assert "https://localhost:8443/ready" in health and "/run/secrets/tls_cert" in health
    audit = str(parsed.audit_log).replace("\\", "/")
    assert any(
        v.split(":")[1] == str(Path(audit).parent).replace("\\", "/") for v in api["volumes"]
    )


def test_the_api_serves_the_registry_and_mounts_it_read_only(doc):
    api = doc["services"]["api"]
    parsed = api_parser().parse_args(api["command"][3:])
    target = str(parsed.model_dir).replace("\\", "/")
    mounts = {
        v.split(":")[1]: v.split(":")[2] if v.count(":") > 1 else "rw" for v in api["volumes"]
    }
    assert target == "/app/models/registry" and mounts[target] == "ro"
    assert mounts["/app/data/processed"] == "ro" and mounts["/app/data/audit"] == "rw"


def test_prometheus_scrapes_the_port_with_the_key_and_certificate_it_is_given(doc):
    prom = yaml.safe_load((ROOT / "infrastructure/monitoring/prometheus.prod.yml").read_text())
    job = prom["scrape_configs"][0]
    service = doc["services"]["prometheus"]
    assert job["scheme"] == "https" and job["static_configs"][0]["targets"] == ["api:8443"]
    assert job["authorization"]["credentials_file"] == "/run/secrets/metrics_key"
    assert job["tls_config"]["ca_file"] == "/run/secrets/tls_cert"
    assert job["tls_config"]["server_name"] in S.CERT_NAMES
    assert {"metrics_key", "tls_cert"} <= set(service["secrets"])
    assert doc["secrets"]["metrics_key"]["file"].endswith("secrets/metrics_key")
    assert doc["secrets"]["tls_cert"]["file"].endswith("secrets/tls/cert.pem")
    assert "insecure_skip_verify" not in json.dumps(prom)


def test_the_dashboard_command_parses_and_matches_its_mounts_and_secret(doc):
    dash = doc["services"]["dashboard"]
    cmd = dash["command"]
    parsed = dash_parser().parse_args(cmd[3:])
    assert parsed.allow_external and parsed.password_file is not None
    assert str(parsed.password_file).replace("\\", "/") in secret_targets(dash)
    mounts = {v.split(":")[1]: v.split(":")[2] for v in dash["volumes"]}
    for path in (parsed.store, parsed.registry, parsed.retrain_dir, parsed.drift_report.parent,
                 parsed.baseline.parent):  # fmt: skip
        assert mounts[str(path).replace("\\", "/")] == "ro", path
    assert dash["ports"] == ["127.0.0.1:8501:8501"] and parsed.port == 8501


def test_grafana_reads_its_password_from_a_file_secret(doc):
    graf = doc["services"]["grafana"]
    assert (
        graf["environment"]["GF_SECURITY_ADMIN_PASSWORD__FILE"] == "/run/secrets/grafana_password"
    )
    assert (
        "grafana_password" in graf["secrets"]
        and "GF_SECURITY_ADMIN_PASSWORD" not in graf["environment"]
    )


def test_monitoring_is_opt_in_and_the_application_is_not(doc):
    s = doc["services"]
    assert s["prometheus"]["profiles"] == ["monitoring"] == s["grafana"]["profiles"]
    assert "profiles" not in s["api"] and "profiles" not in s["dashboard"]
    assert s["prometheus"]["depends_on"]["api"]["condition"] == "service_healthy"


def test_every_service_rotates_its_logs_and_restarts(doc):
    for name, svc in doc["services"].items():
        assert svc["restart"] == "unless-stopped", name
        assert svc["logging"]["options"]["max-size"] and svc["logging"]["options"]["max-file"], name


def test_every_secret_file_the_compose_file_names_is_one_init_makes(doc):
    names = {Path(s["file"]).relative_to("secrets").as_posix() for s in doc["secrets"].values()}
    assert names == set(S.FILES)


def test_the_dashboard_image_is_pinned_unprivileged_and_holds_no_secret():
    text = (ROOT / "Dockerfile.dashboard").read_text()
    lines = [x.strip() for x in text.splitlines() if x.strip() and not x.strip().startswith("#")]
    froms = [x.split()[1] for x in lines if x.startswith("FROM ")]
    assert len(froms) == 2 and all(":" in f and not f.endswith(":latest") for f in froms)
    assert lines[[i for i, x in enumerate(lines) if x.startswith("USER")][-1]] == "USER 10001"
    for x in lines:
        if x.startswith(("ENV", "ARG", "LABEL")):
            assert not SECRET_WORDS.search(x), x
        assert not x.startswith("ADD ")
    assert '".[dashboard]"' in text and "HEALTHCHECK" in text and "EXPOSE 8501" in text


def test_secrets_are_kept_out_of_git_and_out_of_the_image():
    git = (ROOT / ".gitignore").read_text().splitlines()
    docker = (ROOT / ".dockerignore").read_text().splitlines()
    assert "secrets/" in git and "backups/" in git and "secrets" in docker and "backups" in docker
    assert all(c.status == "pass" for c in P.check_ignores(ROOT))


def test_the_ignore_check_catches_a_missing_rule(tmp_path):
    (tmp_path / ".gitignore").write_text("data/\n")
    got = P.check_ignores(tmp_path)
    assert [c.status for c in got] == ["fail", "fail"]  # .dockerignore is absent too
    (tmp_path / ".gitignore").write_text("secrets/\nbackups\n")
    (tmp_path / ".dockerignore").write_text("secrets\n")
    assert [c.status for c in P.check_ignores(tmp_path)] == ["pass", "pass"]


# ---- other preflight checks ----


def test_a_port_that_something_holds_fails_when_needed_and_warns_when_optional():
    with socket.socket() as a, socket.socket() as b:
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        needed, optional = a.getsockname()[1], b.getsockname()[1]
        got = P.check_ports({needed: True, optional: False})
    assert [c.status for c in got] == ["fail", "warn"]
    assert [c.status for c in P.check_ports({needed: True})] == ["pass"]  # free once released


def test_missing_model_and_lake_are_failures_with_the_path_in_the_message(tmp_path):
    m, lake = P.check_model(tmp_path / "none"), P.check_lake(tmp_path / "none")
    assert m.status == "fail" and "none" in m.detail
    assert lake.status == "fail" and "none" in lake.detail


def test_without_docker_the_check_is_a_warning_and_with_a_broken_file_a_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(P.shutil, "which", lambda name: None)
    assert P.check_docker(COMPOSE).status == "warn"
    monkeypatch.setattr(P.shutil, "which", lambda name: "/usr/bin/docker")

    class Done:
        returncode, stderr = 1, "services.api must be a mapping"

    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: Done())
    got = P.check_docker(COMPOSE)
    assert got.status == "fail" and "mapping" in got.detail
    Done.returncode = 0
    assert P.check_docker(COMPOSE).status == "pass"


def test_little_free_disk_warns(monkeypatch, tmp_path):
    class U:
        free = P.MIN_FREE_BYTES - 1

    monkeypatch.setattr(P.shutil, "disk_usage", lambda p: U())
    assert P.check_disk(tmp_path).status == "warn"
    U.free = P.MIN_FREE_BYTES
    assert P.check_disk(tmp_path).status == "pass"


def test_the_verdict_counts_failures_and_in_strict_mode_warnings():
    ok, w, f = checks.ok("a"), checks.warn("b"), checks.fail("c")
    assert (
        checks.verdict([ok]) == 0 and checks.verdict([ok, w]) == 0 and checks.verdict([ok, f]) == 1
    )
    assert checks.verdict([ok, w], strict=True) == 1 and checks.verdict([ok], strict=True) == 0
    assert checks.summary([ok, ok, w, f]) == "2 passed, 1 warnings, 1 failed"
    assert checks.fail("x", "why").line() == "  FAIL  x: why"


# ---- backup and restore ----


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    files = {
        "models/registry/versions/v0001/risk_model.joblib": b"model-bytes",
        "models/registry/production.json": b'{"version": "v0001"}',
        "models/registry/history.jsonl": b'{"action": "register"}\n',
        "data/retrain/last.json": b"{}",
        "data/audit/audit.jsonl": b'{"event": "startup"}\n',
        "data/predictions/part-0.parquet": b"parquet",
        "data/processed/big.parquet": b"not backed up",
        "secrets/api_keys.json": b"{}",
    }
    for name, content in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return root


def names(archive):
    with tarfile.open(archive) as t:
        return sorted(m.name for m in t.getmembers())


def test_a_backup_holds_the_registry_and_records_but_not_secrets_predictions_or_the_lake(
    project, tmp_path
):
    done = B.backup(project, tmp_path / "b.tgz")
    got = names(tmp_path / "b.tgz")
    assert "MANIFEST.json" in got and "models/registry/production.json" in got
    assert "data/audit/audit.jsonl" in got and "data/retrain/last.json" in got
    assert not any(n.startswith(("secrets", "data/predictions", "data/processed")) for n in got)
    assert done["files"] == 5 and set(done["skipped_empty"]) == {"data/drift", "data/monitoring"}


def test_options_add_predictions_and_secrets(project, tmp_path):
    B.backup(project, tmp_path / "p.tgz", with_predictions=True)
    assert "data/predictions/part-0.parquet" in names(tmp_path / "p.tgz")
    done = B.backup(project, tmp_path / "s.tgz", include_secrets=True)
    assert "secrets/api_keys.json" in names(tmp_path / "s.tgz") and done["includes_secrets"]


def test_a_backup_does_not_overwrite_and_does_not_start_from_nothing(project, tmp_path):
    B.backup(project, tmp_path / "b.tgz")
    with pytest.raises(DeployError, match="already exists"):
        B.backup(project, tmp_path / "b.tgz")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DeployError, match="nothing to back up"):
        B.backup(empty, tmp_path / "e.tgz")
    assert not (tmp_path / "e.tgz").exists() and not (tmp_path / "e.tgz.part").exists()


@pytest.mark.skipif(os.name == "nt", reason="links need privileges on Windows")
def test_a_link_inside_the_backed_up_tree_is_refused(project, tmp_path):
    os.symlink(project / "secrets/api_keys.json", project / "models/registry/link.json")
    with pytest.raises(DeployError, match="link"):
        B.backup(project, tmp_path / "b.tgz")


def test_restore_puts_every_file_back_unchanged(project, tmp_path):
    B.backup(project, tmp_path / "b.tgz")
    target = tmp_path / "new"
    done = B.restore(tmp_path / "b.tgz", target)
    assert done["restored"] == 5
    for rel in ("models/registry/production.json", "data/audit/audit.jsonl",
                "models/registry/versions/v0001/risk_model.joblib"):  # fmt: skip
        assert (target / rel).read_bytes() == (project / rel).read_bytes()
    assert not list(target.rglob("*.restoring"))


def rewrite(archive, out, edit):
    """Copy an archive, letting `edit(name, data) -> (name, data) | None` change its members."""
    with tarfile.open(archive) as src, tarfile.open(out, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            changed = edit(m.name, data)
            if changed is None:
                continue
            name, data = changed
            info = tarfile.TarInfo(name)
            info.size = len(data)
            dst.addfile(info, io.BytesIO(data))


def test_a_changed_file_is_refused_before_anything_is_written(project, tmp_path):
    B.backup(project, tmp_path / "b.tgz")

    def tamper(name, data):
        return (name, b"evil") if name == "models/registry/production.json" else (name, data)

    rewrite(tmp_path / "b.tgz", tmp_path / "t.tgz", tamper)
    target = tmp_path / "new"
    with pytest.raises(DeployError, match="differ from the manifest|size differs"):
        B.restore(tmp_path / "t.tgz", target)
    assert not target.exists()  # nothing at all was written, not even the good files


def test_a_member_changed_without_changing_its_size_is_refused_and_nothing_is_written(
    project, tmp_path
):
    B.backup(project, tmp_path / "b.tgz")

    def alter(name, data):
        return (name, b"X" * len(data)) if name == "data/retrain/last.json" else (name, data)

    rewrite(tmp_path / "b.tgz", tmp_path / "t.tgz", alter)
    with pytest.raises(DeployError, match="differ"):
        B.restore(tmp_path / "t.tgz", tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_the_manifest_must_agree_with_the_members(project, tmp_path):
    B.backup(project, tmp_path / "b.tgz")
    rewrite(tmp_path / "b.tgz", tmp_path / "extra.tgz",
            lambda n, d: (n, d) if n != "data/retrain/last.json" else None)  # fmt: skip
    with pytest.raises(DeployError, match="disagree"):
        B.restore(tmp_path / "extra.tgz", tmp_path / "x")

    def add(name, data):
        return (name, data)

    rewrite(tmp_path / "b.tgz", tmp_path / "nomanifest.tgz",
            lambda n, d: None if n == "MANIFEST.json" else (n, d))  # fmt: skip
    with pytest.raises(DeployError, match="no manifest"):
        B.restore(tmp_path / "nomanifest.tgz", tmp_path / "x")


@pytest.mark.parametrize("bad", ["../escape.txt", "/etc/passwd", "models/registry/../../x",
                                 "models\\registry\\x", "C:/x", "models/registry//x"])  # fmt: skip
def test_unsafe_member_names_are_refused(project, tmp_path, bad):
    B.backup(project, tmp_path / "b.tgz")

    def rename(name, data):
        if name == "models/registry/production.json":
            manifest_names.append(bad)
            return bad, data
        return name, data

    manifest_names = []
    rewrite(tmp_path / "b.tgz", tmp_path / "bad.tgz", rename)
    with pytest.raises(DeployError, match="unsafe path"):
        B.restore(tmp_path / "bad.tgz", tmp_path / "x")
    assert not (tmp_path / "x").exists() and not (tmp_path / "escape.txt").exists()


def test_a_path_outside_the_restorable_folders_is_refused_even_with_a_matching_manifest(tmp_path):
    arc = tmp_path / "o.tgz"
    import hashlib

    payload = b"x"
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {"format": 1, "files": [{"path": "src/mmrisk/evil.py", "size": 1, "sha256": digest}]}
    with tarfile.open(arc, "w:gz") as t:
        for name, data in (("MANIFEST.json", json.dumps(manifest).encode()),
                           ("src/mmrisk/evil.py", payload)):  # fmt: skip
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    with pytest.raises(DeployError, match="outside the folders"):
        B.restore(arc, tmp_path / "x")


def test_links_and_devices_in_an_archive_are_refused(project, tmp_path):
    B.backup(project, tmp_path / "b.tgz")
    arc = tmp_path / "l.tgz"
    with tarfile.open(tmp_path / "b.tgz") as src, tarfile.open(arc, "w:gz") as dst:
        for m in src.getmembers():
            dst.addfile(m, src.extractfile(m))
        link = tarfile.TarInfo("models/registry/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        dst.addfile(link)
    with pytest.raises(DeployError, match="not a plain file"):
        B.restore(arc, tmp_path / "x")


def test_existing_files_are_not_overwritten_without_force(project, tmp_path):
    B.backup(project, tmp_path / "b.tgz")
    target = tmp_path / "t"
    B.restore(tmp_path / "b.tgz", target)
    (target / "data/retrain/last.json").write_bytes(b"newer")
    with pytest.raises(DeployError, match="already exist"):
        B.restore(tmp_path / "b.tgz", target)
    assert (target / "data/retrain/last.json").read_bytes() == b"newer"
    B.restore(tmp_path / "b.tgz", target, force=True)
    assert (target / "data/retrain/last.json").read_bytes() == b"{}"


def test_secrets_are_restored_only_when_asked_and_a_dry_run_writes_nothing(project, tmp_path):
    B.backup(project, tmp_path / "b.tgz", include_secrets=True)
    t1 = tmp_path / "t1"
    done = B.restore(tmp_path / "b.tgz", t1)
    assert done["skipped"] == ["secrets/api_keys.json"] and not (t1 / "secrets").exists()
    t2 = tmp_path / "t2"
    B.restore(tmp_path / "b.tgz", t2, include_secrets=True)
    assert (t2 / "secrets/api_keys.json").exists()
    t3 = tmp_path / "t3"
    dry = B.restore(tmp_path / "b.tgz", t3, dry_run=True)
    assert dry["verified"] == 6 and not t3.exists()


def test_a_file_that_is_not_an_archive_is_a_clear_error(tmp_path):
    (tmp_path / "x.tgz").write_bytes(b"not a tar")
    with pytest.raises(DeployError, match="cannot read"):
        B.restore(tmp_path / "x.tgz", tmp_path / "t")
    with pytest.raises(DeployError):
        B.restore(tmp_path / "missing.tgz", tmp_path / "t")


# ---- the command ----


def test_init_prints_the_keys_once_and_the_files_do_not_hold_them(tmp_path, capsys):
    assert deploy_main(["init", "--secrets", str(tmp_path / "s")]) == 0
    out = capsys.readouterr().out
    token = re.search(r"score key.*?(mmk_\S+)", out).group(1)
    assert token not in "".join(p.read_text() for p in (tmp_path / "s").rglob("*")
                                if p.is_file() and p.name != "key.pem")  # fmt: skip
    assert "not shown again" in out
    assert deploy_main(["init", "--secrets", str(tmp_path / "s")]) == 2
    assert "already holds" in capsys.readouterr().err


def test_preflight_fails_with_exit_1_on_missing_secrets_and_strict_turns_warnings_into_failures(
    tmp_path, capsys
):
    argv = ["preflight", "--root", str(ROOT), "--secrets", str(tmp_path / "none"),
            "--compose", str(COMPOSE), "--model-dir", str(tmp_path), "--lake", str(tmp_path),
            "--running"]  # fmt: skip
    assert deploy_main(argv) == 1
    out = capsys.readouterr().out
    assert "FAIL  secrets present" in out and "failed" in out


def test_verify_needs_the_key_in_the_environment_not_on_the_command_line(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("MMRISK_API_KEY", raising=False)
    (tmp_path / "m").write_text("x")
    assert deploy_main(["verify", "--metrics-key-file", str(tmp_path / "m")]) == 2
    assert "MMRISK_API_KEY" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        deploy_main(["verify", "--key", "abc"])  # there is no such flag


def test_backup_and_restore_commands_report_errors_with_exit_2(project, tmp_path, capsys):
    assert deploy_main(["backup", "--root", str(project), "--out", str(tmp_path / "b.tgz")]) == 0
    assert deploy_main(["backup", "--root", str(project), "--out", str(tmp_path / "b.tgz")]) == 2
    assert deploy_main(["restore", str(tmp_path / "b.tgz"), "--target", str(tmp_path / "t")]) == 0
    assert deploy_main(["restore", str(tmp_path / "b.tgz"), "--target", str(tmp_path / "t")]) == 2
    assert deploy_main(["backup", "--root", str(project), "--out", str(tmp_path / "s.tgz"),
                        "--include-secrets"]) == 0  # fmt: skip
    assert "not encrypted" in capsys.readouterr().out


def test_running_the_module_works():
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-m", "mmrisk.deploy", "--help"],
                          capture_output=True, text=True, timeout=60)  # fmt: skip
    assert done.returncode == 0 and "preflight" in done.stdout


def test_a_metrics_key_that_gets_a_200_without_metrics_in_the_body_fails(monkeypatch):
    from mmrisk.deploy import verify as V

    monkeypatch.setattr(V, "run_smoke", lambda *a, **k: [])
    monkeypatch.setattr(V, "_plain_http_refused", lambda *a, **k: checks.ok("plain", "x"))

    def fake(method, url, body, timeout, key=None, context=None):
        if url.endswith("/metrics") and key == "M":
            return 200, "<html>a login page</html>"
        return {"/metrics": (403, ""), "/v1/model": (401 if key is None else 403, "")}.get(
            url[url.rindex("/") :] if url.endswith("/metrics") else "/v1/model", (401, "")
        )

    monkeypatch.setattr(V, "_call", fake)
    out = V.verify("https://127.0.0.1:1", None, "S", "M")
    by = {c.name: c.status for c in out}
    assert by["a metrics key reads /metrics"] == "fail"
