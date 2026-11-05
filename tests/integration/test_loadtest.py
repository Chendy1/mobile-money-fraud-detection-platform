# ruff: noqa: F811, F401
"""Phase 19 against a real API process: the load tool starts it, loads it, and reports."""

import json
import subprocess
import sys

import pytest
from test_api import COLUMNS, data, env, features, model, model_dir, ordered, wire  # noqa: F401

from mmrisk.loadtest import cli as lt  # noqa: E402
from mmrisk.loadtest.cli import main as lt_main  # noqa: E402

PROFILE = ["--profile", "constant", "--rate", "20", "--seconds", "4", "--warmup", "5",
           "--workers", "4", "--last-days", "7"]  # fmt: skip


def stream_start(env):
    args = type("Args", (), {"lake": env[1], "start": None, "last_days": 7.0})
    return lt._stream(args)[0]


def bench(tmp_path, env, model_dir, *extra, name="r"):
    argv = ["bench", *PROFILE, "--model-dir", str(model_dir), "--lake", str(env[1]),
            "--out-dir", str(tmp_path), "--name", name, *extra]  # fmt: skip
    code = lt_main(argv)
    path = tmp_path / f"{name}.json"
    return code, (json.loads(path.read_text()) if path.exists() else None)


def test_a_bench_run_scores_everything_and_the_server_agrees_with_the_client(
    tmp_path, env, model_dir, capsys
):
    code, rep = bench(tmp_path, env, model_dir)
    assert code == 0 and rep["totals"] == {"planned": 80, "sent": 80, "ok": 80, "not_sent": 0}
    seg = rep["segments"][0]
    assert (
        seg["errors"] == {}
        and seg["duplicates"] == 0
        and seg["achieved_rate"] == pytest.approx(20, rel=0.2)
    )
    assert rep["server"] == {"server_200_responses": 85, "client_200_responses": 85, "agree": True}
    assert rep["warnings"] == [] and rep["settings"]["auth"] is False
    assert (tmp_path / "r.md").read_text().startswith("# Load test: r")
    assert "wrote" in capsys.readouterr().out


def test_a_bench_run_with_keys_on_works_with_the_generous_default_limit(tmp_path, env, model_dir):
    code, rep = bench(tmp_path, env, model_dir, "--auth")
    assert code == 0 and rep["totals"]["ok"] == 80 and rep["settings"]["auth"] is True
    assert rep["server"]["agree"] is True  # the admin key may read /metrics


def test_the_rate_limit_shows_up_as_429s_under_load(tmp_path, env, model_dir):
    code, rep = bench(
        tmp_path, env, model_dir, "--auth", "--rate-per-minute", "60", "--rate-burst", "5"
    )
    seg = rep["segments"][0]
    assert code == 0 and seg["errors"].get("rate_limited", 0) >= 60
    assert 1 <= seg["ok"] <= 20 and not seg["slo"]["met"]
    assert rep["settings"]["rate_limit_per_minute"] == 60.0


def test_missing_the_targets_is_exit_1_only_when_asked(tmp_path, env, model_dir):
    code, rep = bench(tmp_path, env, model_dir, "--p95-ms", "0.001", name="a")
    assert code == 0 and not rep["segments"][0]["slo"]["met"]
    code, _ = bench(tmp_path, env, model_dir, "--p95-ms", "0.001", "--fail-on-slo", name="b")
    assert code == 1


def test_a_profile_longer_than_the_stream_is_refused_before_a_server_starts(
    tmp_path, env, model_dir, capsys
):
    argv = ["bench", "--profile", "constant", "--rate", "500", "--seconds", "60"]
    argv += ["--last-days", "1", "--model-dir", str(model_dir), "--lake", str(env[1])]
    argv += ["--out-dir", str(tmp_path)]
    assert lt_main(argv) == 2
    assert "needs 30000 transactions" in capsys.readouterr().err


def test_a_second_run_against_the_same_server_is_flagged_as_duplicates(
    tmp_path, env, model_dir, capsys
):
    port = lt._free_port()
    url = f"http://127.0.0.1:{port}"
    start = stream_start(env)
    log = tmp_path / "server.log"
    with open(log, "w") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mmrisk.api", "serve", "--model-dir", str(model_dir),
             "--lake", str(env[1]), "--port", str(port), "--history-until", start.isoformat(),
             "--log-level", "warning"],
            stdout=out, stderr=subprocess.STDOUT,
        )  # fmt: skip
        try:
            lt._wait_ready(url, proc, log)
            argv = ["run", *PROFILE, "--url", url, "--from", start.isoformat(),
                    "--lake", str(env[1]), "--out-dir", str(tmp_path)]  # fmt: skip
            assert lt_main([*argv, "--name", "first"]) == 0
            assert lt_main([*argv, "--name", "second"]) == 0
        finally:
            proc.terminate()
            proc.wait(timeout=15)
    first = json.loads((tmp_path / "first.json").read_text())
    second = json.loads((tmp_path / "second.json").read_text())
    assert first["warnings"] == [] and first["segments"][0]["duplicates"] == 0
    assert second["segments"][0]["duplicates"] == 80 and any(
        "duplicate" in w for w in second["warnings"]
    )
    assert "restart" in capsys.readouterr().out


def test_nothing_listening_is_a_clear_error_not_a_hang(tmp_path, env, model_dir, capsys):
    port = lt._free_port()
    start = stream_start(env)
    argv = ["run", *PROFILE, "--url", f"http://127.0.0.1:{port}", "--timeout", "1"]
    argv += ["--from", start.isoformat(), "--lake", str(env[1]), "--out-dir", str(tmp_path)]
    code = lt_main(argv)
    assert code == 2 and "no answer" in capsys.readouterr().err


def test_the_in_process_timing_reports_percentiles_and_a_ceiling(tmp_path, env, model_dir, capsys):
    code = lt_main(["inproc", "--model-dir", str(model_dir), "--lake", str(env[1]),
                    "--requests", "60", "--out-dir", str(tmp_path), "--name", "ip"])  # fmt: skip
    rep = json.loads((tmp_path / "ip.json").read_text())
    lat = rep["latency_ms"]
    assert code == 0 and rep["scored"] == 60
    assert 0 < lat["p50"] <= lat["p95"] <= lat["p99"] <= lat["max"]
    assert rep["single_thread_ceiling_per_s"] == pytest.approx(1000 / lat["mean"])
