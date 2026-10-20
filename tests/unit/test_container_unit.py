"""The container files, checked without Docker: security settings and agreement between them."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from mmrisk.api.cli import build_parser  # noqa: E402
from mmrisk.streaming.cli import build_parser as stream_parser  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SECRET_WORDS = re.compile(r"(PASSWORD|SECRET|TOKEN|API_?KEY|CREDENTIAL)", re.I)


def instructions(text: str) -> list[tuple[str, str]]:
    """Dockerfile instructions as (KEYWORD, rest), with continuation lines joined."""
    joined = re.sub(r"\\\s*\n", " ", text)
    out = []
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keyword, _, rest = line.partition(" ")
            out.append((keyword.upper(), rest.strip()))
    return out


@pytest.fixture(scope="module")
def docker():
    return instructions((ROOT / "Dockerfile").read_text())


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


@pytest.fixture(scope="module")
def api(compose):
    return compose["services"]["api"]


def cmd_args(docker) -> list[str]:
    cmd = [rest for kw, rest in docker if kw == "CMD"]
    assert len(cmd) == 1 and cmd[0].startswith("["), "CMD must be written in exec (JSON) form"
    return json.loads(cmd[0])


# ---- the image ----


def test_every_base_image_is_a_specific_tag(docker):
    bases = [rest.split()[0] for kw, rest in docker if kw == "FROM"]
    assert len(bases) == 2  # a build stage and a runtime stage
    for image in bases:
        assert ":" in image and not image.endswith(":latest"), image


def test_the_runtime_stage_runs_as_a_numeric_non_root_user(docker):
    users = [rest for kw, rest in docker if kw == "USER"]
    assert users and users[-1].isdigit() and int(users[-1]) > 0
    last_from = max(i for i, (kw, _) in enumerate(docker) if kw == "FROM")
    assert any(kw == "USER" for kw, _ in docker[last_from:])  # set after the final FROM


def test_nothing_secret_is_baked_into_the_image(docker):
    for kw, rest in docker:
        if kw in ("ENV", "ARG", "LABEL"):
            assert not SECRET_WORDS.search(rest), (kw, rest)
        assert kw != "ADD", "ADD can fetch URLs and unpack archives; use COPY"
    assert not any("curl " in rest or "wget " in rest for _, rest in docker)


def test_only_the_files_the_build_needs_are_copied(docker):
    copies = [rest for kw, rest in docker if kw == "COPY" and "--from" not in rest]
    assert copies == ["pyproject.toml README.md ./", "src ./src"]
    for name in ("pyproject.toml", "README.md", "src"):
        assert (ROOT / name).exists(), name


def test_it_installs_the_serving_dependencies_only(docker):
    runs = " ".join(rest for kw, rest in docker if kw == "RUN")
    assert '".[serve]"' in runs
    for extra in ("dev", "eda", "ml", "explain", "tracking", "api"):
        assert f"[{extra}" not in runs and f",{extra}" not in runs
    apt = re.findall(r"apt-get install[^&]*", runs)
    assert len(apt) == 1 and "--no-install-recommends" in apt[0] and "libgomp1" in apt[0]
    assert apt[0].split("libgomp1")[1].strip() == ""  # nothing else is installed
    assert "rm -rf /var/lib/apt/lists" in runs


def test_the_serve_extra_has_what_a_saved_model_needs():
    text = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"^serve = \[(.*?)\]", text, re.M | re.S).group(1)
    for package in ("scikit-learn", "lightgbm", "fastapi", "uvicorn"):
        assert package in block
    for heavy in ("matplotlib", "shap", "mlflow", "duckdb", "pytest"):
        assert heavy not in block


def test_the_command_is_a_valid_serve_command_for_the_container(docker):
    args = cmd_args(docker)
    assert args[:3] == ["python", "-m", "mmrisk.api"]
    parsed = build_parser().parse_args(args[3:])  # fails if a flag was renamed or removed
    assert parsed.cmd == "serve" and parsed.host == "0.0.0.0" and parsed.allow_external
    assert str(parsed.model_dir).replace("\\", "/") == "/app/models/advanced"
    assert str(parsed.lake).replace("\\", "/") == "/app/data/processed"
    assert parsed.port == 8000


def test_the_healthcheck_asks_the_port_the_command_listens_on(docker):
    health = [rest for kw, rest in docker if kw == "HEALTHCHECK"]
    assert len(health) == 1 and "/ready" in health[0] and "--start-period" in health[0]
    port = build_parser().parse_args(cmd_args(docker)[3:]).port
    assert f"127.0.0.1:{port}" in health[0]
    exposed = [rest for kw, rest in docker if kw == "EXPOSE"]
    assert exposed == [str(port)]


def test_the_healthcheck_command_exits_non_zero_when_the_service_is_not_ready(docker):
    health = [rest for kw, rest in docker if kw == "HEALTHCHECK"][0]
    command = json.loads(health[health.index("CMD") + 4 :])
    assert command[:2] == ["python", "-c"] and "urlopen" in command[2]
    assert "mmrisk" not in command[2]  # standard library only: cheap to run every 30 seconds
    code = command[2].replace("127.0.0.1:8000", "127.0.0.1:1")  # nothing listens there
    done = subprocess.run(["python", "-c", code], capture_output=True, timeout=30)
    assert done.returncode != 0


# ---- the compose file ----


def test_the_port_is_published_on_the_loopback_address_only(api):
    assert api["ports"]
    for mapping in api["ports"]:
        assert str(mapping).startswith("127.0.0.1:"), mapping


def test_the_container_is_locked_down(api):
    assert api["read_only"] is True and api["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in api["security_opt"]
    for forbidden in ("privileged", "network_mode", "pid", "ipc", "cap_add", "devices"):
        assert forbidden not in api, forbidden
    assert "/tmp" in api["tmpfs"]


def test_it_has_resource_limits(api):
    assert api["mem_limit"] and api["cpus"] and api["pids_limit"]


def test_mounts_are_read_only_and_match_what_the_command_reads(api, docker):
    parsed = build_parser().parse_args(cmd_args(docker)[3:])
    targets = set()
    for volume in api["volumes"]:
        source, target, mode = volume.split(":")
        assert mode == "ro", volume
        assert "docker.sock" not in source and not source.startswith(("/", "~")), volume
        targets.add(target)
    assert targets == {
        str(parsed.model_dir).replace("\\", "/"),
        str(parsed.lake).replace("\\", "/"),
    }


def test_no_secrets_are_set_in_the_compose_file(api):
    assert "env_file" not in api
    for name, _ in _env_items(api.get("environment")):
        assert not SECRET_WORDS.search(name), name


def _env_items(env):
    if not env:
        return []
    if isinstance(env, dict):
        return list(env.items())
    return [tuple(item.split("=", 1)) if "=" in item else (item, "") for item in env]


def test_the_image_name_is_not_latest_and_the_build_points_at_the_dockerfile(api):
    assert not api["image"].endswith(":latest") and ":" in api["image"]
    assert api["build"]["dockerfile"] == "Dockerfile" and (ROOT / "Dockerfile").exists()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_docker_compose_accepts_the_file():
    done = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "config", "-q"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    if "is not a docker command" in done.stderr or "unknown shorthand" in done.stderr:
        pytest.skip("the docker compose plugin is not installed")
    assert done.returncode == 0, done.stderr


# ---- what stays out of the image ----


@pytest.mark.parametrize(
    "entry",
    [
        ".git",
        ".env",
        ".venv",
        "data",
        "models",
        "mlruns",
        "*.parquet",
        "*.joblib",
        "*.pem",
        "*.key",
    ],
)
def test_the_build_context_excludes_local_data_models_and_secrets(entry):
    lines = {x.strip() for x in (ROOT / ".dockerignore").read_text().splitlines()}
    assert entry in lines, entry


def test_the_env_example_is_the_only_env_file_allowed_into_the_context():
    lines = [x.strip() for x in (ROOT / ".dockerignore").read_text().splitlines()]
    assert ".env.*" in lines and "!.env.example" in lines


# ---- the simulator service (Phase 13) ----


@pytest.fixture(scope="module")
def simulator(compose):
    return compose["services"]["simulator"]


def test_the_simulator_is_opt_in_and_waits_for_a_healthy_api(simulator):
    assert simulator["profiles"] == ["simulate"]
    assert simulator["depends_on"]["api"]["condition"] == "service_healthy"


def test_the_api_service_starts_by_default(api):
    assert "profiles" not in api


def test_the_simulator_is_locked_down_like_the_api(simulator):
    assert simulator["read_only"] is True and simulator["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in simulator["security_opt"]
    for forbidden in ("privileged", "network_mode", "pid", "ipc", "cap_add", "devices", "ports"):
        assert forbidden not in simulator, forbidden
    assert "/tmp" in simulator["tmpfs"]
    assert simulator["mem_limit"] and simulator["cpus"] and simulator["pids_limit"]
    assert "env_file" not in simulator


def test_the_simulator_command_parses_and_matches_its_mounts(simulator, api):
    args = stream_parser().parse_args(simulator["command"][3:])
    assert simulator["command"][:3] == ["python", "-m", "mmrisk.streaming"]
    assert args.cmd == "run" and args.url == "http://api:8000"
    assert args.url.endswith(":8000")  # the port the api container listens on
    mounts = {}
    for volume in simulator["volumes"]:
        source, target, *mode = volume.split(":")
        assert not source.startswith(("/", "~")) and "docker.sock" not in source, volume
        mounts[target] = mode[0] if mode else "rw"
    assert mounts[str(args.lake).replace("\\", "/")] == "ro"
    assert mounts[str(args.store).replace("\\", "/")] == "rw"
    assert len(mounts) == 2  # the model is not needed: scoring is done by the api


def test_the_simulator_uses_the_same_image_as_the_api(simulator, api):
    assert simulator["image"] == api["image"] and simulator["build"] == api["build"]


def test_the_stream_start_reaches_both_services_from_one_variable(simulator, api):
    assert dict(_env_items(api["environment"]))["MMRISK_HISTORY_UNTIL"] == "${STREAM_FROM:-}"
    assert "${STREAM_FROM:-}" in simulator["command"]


def test_no_secrets_are_set_for_the_simulator(simulator):
    for name, _ in _env_items(simulator.get("environment")):
        assert not SECRET_WORDS.search(name), name


def test_the_api_history_cut_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("MMRISK_HISTORY_UNTIL", "2025-12-24T00:00:00")
    assert build_parser().parse_args(["serve"]).history_until == "2025-12-24T00:00:00"
    monkeypatch.setenv("MMRISK_HISTORY_UNTIL", "")
    assert not build_parser().parse_args(["serve"]).history_until  # empty means everything
    monkeypatch.delenv("MMRISK_HISTORY_UNTIL")
    assert not build_parser().parse_args(["serve"]).history_until
    monkeypatch.setenv("MMRISK_HISTORY_UNTIL", "2025-12-24T00:00:00")
    assert (
        build_parser().parse_args(["serve", "--history-until", "2025-01-01"]).history_until
        == "2025-01-01"
    )  # a flag wins
