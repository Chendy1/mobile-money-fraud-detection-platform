"""The Prometheus and Grafana files, checked without Docker: they must agree with the API."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
pytest.importorskip("fastapi")
pytest.importorskip("prometheus_client")

from mmrisk.api.app import create_app  # noqa: E402
from mmrisk.monitoring.metrics import ACTIONS, metric_names  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MON = ROOT / "infrastructure" / "monitoring"
NAME = re.compile(r"\bmmrisk_[a-z0-9_]+")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]


@pytest.fixture(scope="module")
def prom():
    return yaml.safe_load((MON / "prometheus.yml").read_text())


@pytest.fixture(scope="module")
def rules():
    return yaml.safe_load((MON / "alerts.yml").read_text())


@pytest.fixture(scope="module")
def dashboard():
    return json.loads((MON / "grafana/dashboards/mmrisk.json").read_text())


@pytest.fixture(scope="module")
def routes():
    return {r.path for r in create_app(None).routes}


def targets(dashboard):
    return [t for p in dashboard["panels"] for t in p["targets"]]


def alert_rules(rules):
    return [r for g in rules["groups"] for r in g["rules"]]


def mounts(service):
    out = {}
    for v in service["volumes"]:
        source, target, *mode = v.split(":")
        out[target] = (source, mode[0] if mode else "rw")
    return out


# ---- Prometheus ----


def test_prometheus_scrapes_the_api_service_on_its_port_and_path(prom, compose):
    job = prom["scrape_configs"][0]
    host, port = job["static_configs"][0]["targets"][0].split(":")
    assert host in compose and compose[host].get("profiles") is None  # the default service
    assert port == "8000" and any("8000" in str(p) for p in compose[host]["ports"])
    assert job["metrics_path"] == "/metrics" and "/metrics" in {
        r.path for r in create_app(None).routes
    }
    assert job["job_name"] == "mmrisk-api"


def test_prometheus_loads_the_rules_file_that_is_mounted(prom, compose):
    mount = mounts(compose["prometheus"])
    assert prom["rule_files"] == ["/etc/prometheus/alerts.yml"]
    assert mount["/etc/prometheus/alerts.yml"] == ("./infrastructure/monitoring/alerts.yml", "ro")
    assert mount["/etc/prometheus/prometheus.yml"][1] == "ro"
    assert "--config.file=/etc/prometheus/prometheus.yml" in compose["prometheus"]["command"]


def test_alert_rules_are_complete_and_unique(rules):
    found = alert_rules(rules)
    names = [r["alert"] for r in found]
    assert len(found) >= 6 and len(set(names)) == len(names)
    for r in found:
        assert r["expr"].strip() and r["labels"]["severity"] in {"warning", "critical"}
        assert r["annotations"]["summary"].strip()
        assert "for" in r  # a single scrape never fires an alert


def test_alert_expressions_use_only_metrics_the_api_exposes(rules, routes):
    known = metric_names() | {"up"}
    for r in alert_rules(rules):
        for name in NAME.findall(r["expr"]):
            assert name in known, (r["alert"], name)
        for path in re.findall(r'endpoint="([^"]+)"', r["expr"]):
            assert path in routes, (r["alert"], path)
        for action in re.findall(r'action!?="([^"]+)"', r["expr"]):
            assert action in ACTIONS


def test_alert_expressions_have_balanced_brackets_and_quotes(rules):
    for r in alert_rules(rules):
        e = r["expr"]
        assert e.count("(") == e.count(")") and e.count("[") == e.count("]")
        assert e.count("{") == e.count("}") and e.count('"') % 2 == 0, r["alert"]


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool is not installed")
def test_promtool_accepts_the_files():
    for args in (["check", "rules", str(MON / "alerts.yml")],
                 ["check", "config", str(MON / "prometheus.yml")]):  # fmt: skip
        done = subprocess.run(["promtool", *args], capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stdout + done.stderr


# ---- Grafana ----


def test_the_dashboard_is_well_formed(dashboard):
    assert dashboard["uid"] == "mmrisk-api" and dashboard["editable"] is False
    panels = dashboard["panels"]
    ids = [p["id"] for p in panels]
    titles = [p["title"] for p in panels]
    assert len(set(ids)) == len(ids) and len(set(titles)) == len(titles)
    taken = set()
    for p in panels:
        g = p["gridPos"]
        assert g["x"] >= 0 and g["w"] > 0 and g["x"] + g["w"] <= 24 and g["h"] > 0
        cells = {
            (x, y) for x in range(g["x"], g["x"] + g["w"]) for y in range(g["y"], g["y"] + g["h"])
        }
        assert not cells & taken, f"panel {p['title']} overlaps another"
        taken |= cells
        assert p["targets"] and all(t["expr"].strip() for t in p["targets"])
        assert len({t["refId"] for t in p["targets"]}) == len(p["targets"])


def test_the_dashboard_queries_use_only_metrics_and_labels_the_api_has(dashboard, routes):
    known = metric_names()
    for t in targets(dashboard):
        e = t["expr"]
        for name in NAME.findall(e):
            assert name in known, (e, name)
        assert e.count("(") == e.count(")") and e.count("{") == e.count("}")
        for path in re.findall(r'endpoint="([^"]+)"', e):
            assert path in routes
        for action in re.findall(r'action!?="([^"]+)"', e):
            assert action in ACTIONS


def test_the_dashboard_and_datasource_agree(dashboard):
    ds = yaml.safe_load((MON / "grafana/provisioning/datasources/prometheus.yml").read_text())
    source = ds["datasources"][0]
    assert source["uid"] == "prometheus" and source["type"] == "prometheus"
    assert source["url"] == "http://prometheus:9090"
    for p in dashboard["panels"]:
        assert p["datasource"]["uid"] == "prometheus"
        assert all(t["datasource"]["uid"] == "prometheus" for t in p["targets"])


def test_the_dashboard_folder_is_mounted_where_the_provider_reads_it(compose):
    provider = yaml.safe_load((MON / "grafana/provisioning/dashboards/dashboards.yml").read_text())[
        "providers"
    ][0]
    mount = mounts(compose["grafana"])
    path = provider["options"]["path"]
    assert mount[path] == ("./infrastructure/monitoring/grafana/dashboards", "ro")
    assert mount["/etc/grafana/provisioning"][1] == "ro"
    assert provider["allowUiUpdates"] is False


# ---- the compose services ----


@pytest.mark.parametrize("name", ["prometheus", "grafana"])
def test_the_monitoring_services_are_opt_in_and_locked_down(compose, name):
    s = compose[name]
    assert s["profiles"] == ["monitoring"]
    assert s["read_only"] is True and s["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in s["security_opt"] and "/tmp" in s["tmpfs"]
    assert s["mem_limit"] and s["cpus"] and s["pids_limit"]
    for forbidden in ("privileged", "network_mode", "pid", "ipc", "cap_add", "devices", "user"):
        assert forbidden not in s, forbidden
    assert s["image"].count(":") == 1 and not s["image"].endswith(":latest")
    for mapping in s["ports"]:
        assert str(mapping).startswith("127.0.0.1:"), mapping
    for target, (source, mode) in mounts(s).items():
        assert "docker.sock" not in source
        if source.startswith("."):
            assert mode == "ro", target  # configuration is read-only; data is a named volume


def test_the_api_does_not_depend_on_monitoring_and_starts_without_it(compose):
    api = compose["api"]
    assert "profiles" not in api and "depends_on" not in api
    assert compose["grafana"]["depends_on"] == ["prometheus"]


def test_prometheus_keeps_two_days_and_writes_only_to_its_volume(compose):
    s = compose["prometheus"]
    assert "--storage.tsdb.retention.time=2d" in s["command"]
    assert "--storage.tsdb.path=/prometheus" in s["command"]
    assert mounts(s)["/prometheus"] == ("prometheus-data", "rw")


def test_grafana_has_no_default_password_and_no_anonymous_access(compose):
    env = compose["grafana"]["environment"]
    assert env["GF_SECURITY_ADMIN_PASSWORD"] == "${GRAFANA_ADMIN_PASSWORD:-}"  # from the shell only
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "false" and env["GF_USERS_ALLOW_SIGN_UP"] == "false"
    assert env["GF_ANALYTICS_REPORTING_ENABLED"] == "false"


def test_no_service_has_a_literal_secret_in_its_environment(compose):
    secret = re.compile(r"(PASSWORD|SECRET|TOKEN|API_?KEY|CREDENTIAL)", re.I)
    for name, service in compose.items():
        env = service.get("environment") or {}
        env = env if isinstance(env, dict) else dict(e.split("=", 1) for e in env)
        for key, value in env.items():
            if secret.search(key):
                assert str(value).startswith("${") or value in ("", None), (name, key)


def test_the_grafana_guard_is_written_before_the_original_start_command(compose):
    command = compose["grafana"]["entrypoint"][2]
    assert command.startswith("test -n") and command.rstrip().endswith("exec /run.sh")
    assert "GRAFANA_ADMIN_PASSWORD" in command  # the message names the variable to set


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell (Windows without Git Bash)")
def test_grafana_refuses_to_start_without_a_password(compose):
    script = (
        compose["grafana"]["entrypoint"][2]
        .replace("$$", "$")
        .replace("exec /run.sh", "echo started")
    )
    inherited = {k: v for k, v in os.environ.items() if k != "GF_SECURITY_ADMIN_PASSWORD"}
    for extra, code in (
        ({}, 1),
        ({"GF_SECURITY_ADMIN_PASSWORD": ""}, 1),
        ({"GF_SECURITY_ADMIN_PASSWORD": "x"}, 0),
    ):
        done = subprocess.run(
            [shutil.which("sh"), "-c", script],
            env={**inherited, **extra}, capture_output=True, text=True,
        )  # fmt: skip
        assert done.returncode == code, extra


def test_the_named_volumes_are_declared():
    top = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert {"prometheus-data", "grafana-data"} <= set(top["volumes"])


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
@pytest.mark.parametrize("profile", [[], ["--profile", "monitoring"]])
def test_docker_compose_accepts_the_file_with_and_without_monitoring(profile):
    done = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), *profile, "config", "-q"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    if "is not a docker command" in done.stderr or "unknown shorthand" in done.stderr:
        pytest.skip("the docker compose plugin is not installed")
    assert done.returncode == 0, done.stderr


def test_every_query_parses_as_promql(rules, dashboard):
    promql = pytest.importorskip("promql_parser")
    queries = [r["expr"] for r in alert_rules(rules)] + [t["expr"] for t in targets(dashboard)]
    assert len(queries) >= 15
    for q in queries:
        promql.parse(q)  # raises ValueError on a syntax error
    with pytest.raises(ValueError):
        promql.parse("sum(rate(mmrisk_http_requests_total[5m])")  # the parser really checks
