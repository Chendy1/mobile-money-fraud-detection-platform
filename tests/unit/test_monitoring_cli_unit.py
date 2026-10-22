"""python -m mmrisk.monitoring: exit codes, files written, and refusal of bad input."""

import json
from datetime import datetime, timedelta

import polars as pl
import pytest
from test_monitoring_checks_unit import frame

pytest.importorskip("fastapi")  # the streaming package (store reader) imports the API schemas

from mmrisk.monitoring.cli import build_parser, main  # noqa: E402


def make_store(path, df):
    path.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path / "part-000001.parquet")
    return path


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path / "pred", frame(n=4000))


def run(*args):
    return main([str(a) for a in args])


def test_baseline_writes_a_readable_file_and_leaves_no_temporary_file(store, tmp_path, capsys):
    out = tmp_path / "mon"
    assert run("baseline", "--store", store, "--out", out) == 0
    data = json.loads((out / "baseline.json").read_text())
    assert data["rows"] == 4000 and data["flagged_share"] == pytest.approx(0.025)
    assert [p.name for p in out.iterdir()] == ["baseline.json"]
    assert "flagged share 2.50%" in capsys.readouterr().out


def test_a_healthy_report_exits_zero_and_writes_markdown_and_json(store, tmp_path, capsys):
    out = tmp_path / "mon"
    assert run("report", "--store", store, "--out", out) == 0
    text = capsys.readouterr().out
    assert "overall: PASS" in text and "skip  store is fresh" in text
    md = (out / "report.md").read_text()
    assert "# Prediction monitoring report" in md and "| Check | Result | Detail |" in md
    assert "| 2025-12-01 00:00:00 |" in md  # the windows table
    assert "Baseline: none" in md and "Synthetic data only" in md
    data = json.loads((out / "report.json").read_text())
    assert data["overall"] == "PASS" and data["checks"][0]["name"] == "predictions exist"
    assert sorted(p.name for p in out.iterdir()) == ["report.json", "report.md"]


def test_a_warning_exits_zero_and_a_failure_exits_one(tmp_path, capsys):
    mixed = frame(n=4000).with_columns(
        pl.when(pl.col("seq") < 10)
        .then(pl.lit("2.0"))
        .otherwise(pl.col("feature_version"))
        .alias("feature_version")
    )
    warn = make_store(tmp_path / "w", mixed)
    assert run("report", "--store", warn, "--out", tmp_path / "o1") == 0
    assert "overall: WARN" in capsys.readouterr().out
    dup = make_store(
        tmp_path / "f", frame(n=4000).with_columns(pl.lit("T1").alias("transaction_id"))
    )
    assert run("report", "--store", dup, "--out", tmp_path / "o2") == 1
    assert "overall: FAIL" in capsys.readouterr().out
    assert (tmp_path / "o2" / "report.md").exists()  # a failing report is still written


def test_the_baseline_and_options_are_used(store, tmp_path, capsys):
    out = tmp_path / "mon"
    run("baseline", "--store", store, "--out", out)
    capsys.readouterr()
    late = (datetime(2026, 1, 1) + timedelta(days=30)).isoformat()
    args = ("report", "--store", store, "--out", out, "--baseline", out / "baseline.json")
    assert run(*args, "--window", "1d", "--review-capacity-per-hour", "100", "--as-of", late) == 0
    text = capsys.readouterr().out
    assert "PASS  flagged share like the baseline" in text
    assert "PASS  review queue within capacity" in text
    assert "WARN  store is fresh" in text  # the newest prediction is a month older than --as-of


def test_user_mistakes_exit_two_and_say_what_is_wrong(store, tmp_path, capsys):
    out = tmp_path / "o"
    empty = tmp_path / "empty"
    empty.mkdir()
    bad_json = tmp_path / "b.json"
    bad_json.write_text("{not json")
    wrong_shape = tmp_path / "w.json"
    wrong_shape.write_text('{"flagged_share": 0.1}')
    cases = [
        (("report", "--store", empty, "--out", out), "no predictions"),
        (("report", "--store", tmp_path / "missing", "--out", out), "no predictions"),
        (("report", "--store", store, "--out", out, "--window", "2w"), "window"),
        (
            ("report", "--store", store, "--out", out, "--baseline", tmp_path / "nope.json"),
            "baseline",
        ),
        (("report", "--store", store, "--out", out, "--baseline", bad_json), "baseline"),
        (("report", "--store", store, "--out", out, "--baseline", wrong_shape), "not a baseline"),
        (("report", "--store", store, "--out", out, "--review-capacity-per-hour", "0"), "capacity"),
        (("report", "--store", store, "--out", out, "--as-of", "yesterday"), ""),
        (("baseline", "--store", empty, "--out", out), "no predictions"),
    ]
    for args, word in cases:
        assert run(*args) == 2, args
        assert word in capsys.readouterr().err, args
    assert not out.exists()  # nothing was written for a mistake


def test_the_files_the_command_writes_are_not_mistaken_for_predictions(store, tmp_path):
    # the report folder may sit beside or inside the store; only part files are predictions
    run("report", "--store", store, "--out", store)
    assert run("report", "--store", store, "--out", store) == 0
    from mmrisk.streaming.store import parts

    assert len(parts(store)) == 1


def test_the_defaults_point_at_the_streaming_output():
    args = build_parser().parse_args(["report"])
    assert str(args.store).replace("\\", "/") == "data/predictions"
    assert str(args.out).replace("\\", "/") == "data/monitoring" and args.window == "1d"
    assert args.baseline is None and args.as_of is None
