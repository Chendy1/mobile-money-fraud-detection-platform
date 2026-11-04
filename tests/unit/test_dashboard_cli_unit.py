"""The dashboard launcher: it must stay on this computer unless told otherwise."""

import sys

import pytest
from dashboard_helpers import make_registry, make_store

from mmrisk.dashboard import cli
from mmrisk.dashboard.cli import main
from mmrisk.security.passwords import hash_password


@pytest.fixture
def launched(monkeypatch):
    calls = []
    pytest.importorskip("streamlit")
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd, env=None: calls.append((cmd, env)) or 0)
    return calls


def test_run_binds_to_loopback_without_telemetry(launched, tmp_path):
    assert main(["run", "--store", str(tmp_path / "p"), "--registry", str(tmp_path / "r")]) == 0
    cmd, env = launched[0]
    assert cmd[:4] == [sys.executable, "-m", "streamlit", "run"] and cmd[4].endswith("app.py")
    assert cmd[cmd.index("--server.address") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--server.port") + 1] == "8501"
    assert cmd[cmd.index("--browser.gatherUsageStats") + 1] == "false"
    assert env["MMRISK_DASH_STORE"] == str(tmp_path / "p")
    assert env["MMRISK_DASH_REGISTRY"] == str(tmp_path / "r")
    assert {"MMRISK_DASH_BASELINE", "MMRISK_DASH_DRIFT", "MMRISK_DASH_RETRAIN"} <= env.keys()


def test_run_refuses_a_network_address_unless_allowed(launched, capsys, tmp_path):
    assert main(["run", "--host", "0.0.0.0"]) == 2
    assert "--password-file" in capsys.readouterr().err and not launched
    assert main(["run", "--host", "0.0.0.0", "--allow-external"]) == 2  # still no login
    assert not launched
    hashfile = tmp_path / "dash.hash"
    hashfile.write_text(hash_password("a long enough pass"), encoding="utf-8")
    argv = ["run", "--host", "0.0.0.0", "--allow-external", "--password-file", str(hashfile)]
    assert main(argv) == 0
    assert launched[0][0][launched[0][0].index("--server.address") + 1] == "0.0.0.0"
    assert launched[0][1]["MMRISK_DASH_PASSWORD_HASH"] == hashfile.read_text().strip()
    assert (
        main([a for a in argv if a != "--allow-external"]) == 2
    )  # a password file without --allow-external is not enough either
    assert main(["run", "--host", "localhost"]) == 0 and main(["run", "--host", "::1"]) == 0


def test_run_checks_the_port_and_the_install(launched, monkeypatch, capsys):
    assert main(["run", "--port", "0"]) == 2 and main(["run", "--port", "70000"]) == 2
    monkeypatch.setitem(sys.modules, "streamlit", None)  # as if it were not installed
    assert main(["run"]) == 2
    assert "dashboard]" in capsys.readouterr().err
    assert not launched


def test_run_headless_flag(launched):
    main(["run", "--no-browser"])
    cmd = launched[0][0]
    assert cmd[cmd.index("--server.headless") + 1] == "true"
    main(["run"])
    assert launched[1][0][launched[1][0].index("--server.headless") + 1] == "false"


def test_check_reports_found_and_missing(tmp_path, capsys):
    store = make_store(tmp_path / "p", n=100)
    reg = make_registry(tmp_path / "r", tmp_path)
    args = ["check", "--store", str(store), "--registry", str(reg.root), "--baseline",
            str(tmp_path / "none.json"), "--drift-report", str(tmp_path / "d.json"),
            "--retrain-dir", str(tmp_path / "t")]  # fmt: skip
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "found   predictions" in out and "missing monitoring baseline" in out
    assert "missing drift report" in out and "found   model registry" in out
    assert "predictions: 100 rows" in out and "models: 3 versions, production v0001" in out
    assert "health: " in out


def test_check_with_nothing_present_still_exits_0(tmp_path, capsys):
    base = ["--store", str(tmp_path / "a"), "--registry", str(tmp_path / "b")]
    assert main(["check", *base]) == 0
    out = capsys.readouterr().out
    assert "predictions: no predictions" in out and "models: no model registry" in out


def test_window_must_be_a_known_one():
    with pytest.raises(SystemExit):
        main(["check", "--window", "2x"])


def test_the_launcher_never_runs_the_page_itself(launched):
    """Importing app.py runs the whole page; the launcher must not do that."""
    for name in [m for m in sys.modules if m.startswith("mmrisk.dashboard.app")]:
        del sys.modules[name]
    assert main(["run"]) == 0 and main(["check", "--store", "none"]) == 0
    assert "mmrisk.dashboard.app" not in sys.modules


def test_ctrl_c_stops_it_quietly(monkeypatch, capsys):
    pytest.importorskip("streamlit")

    def interrupted(cmd, env=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.subprocess, "call", interrupted)
    assert main(["run"]) == 0
    assert "dashboard stopped" in capsys.readouterr().out


def test_a_bad_or_missing_password_file_is_refused_and_a_stale_hash_is_not_inherited(
    launched, tmp_path, monkeypatch, capsys
):
    assert main(["run", "--password-file", str(tmp_path / "none")]) == 2
    (tmp_path / "bad").write_text("not a hash")
    assert main(["run", "--password-file", str(tmp_path / "bad")]) == 2
    assert "dash-password" in capsys.readouterr().err and not launched
    monkeypatch.setenv("MMRISK_DASH_PASSWORD_HASH", "leftover")
    assert main(["run"]) == 0
    assert "MMRISK_DASH_PASSWORD_HASH" not in launched[0][1]
