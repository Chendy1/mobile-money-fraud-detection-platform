"""Phase 13 tests with a trained model: the stream must score exactly as batch scoring does."""

import json
import socket
import threading
import time
from datetime import date, datetime

import polars as pl
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")
pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from mmrisk.api import ScoringService  # noqa: E402
from mmrisk.api.app import create_app  # noqa: E402
from mmrisk.features import compute_features  # noqa: E402
from mmrisk.generation import GeneratorConfig, generate  # noqa: E402
from mmrisk.ingestion import ingest_source_dir, read_layer  # noqa: E402
from mmrisk.models.boosting import fit_lightgbm  # noqa: E402
from mmrisk.models.bundle import RiskModel  # noqa: E402
from mmrisk.models.calibration import PlattCalibrator  # noqa: E402
from mmrisk.models.costs import CostModel  # noqa: E402
from mmrisk.models.split import make_split  # noqa: E402
from mmrisk.streaming import (  # noqa: E402
    FaultConfig,
    HttpScorer,
    LocalScorer,
    PredictionStore,
    StreamConfig,
    run_stream,
)
from mmrisk.streaming.cli import main as cli  # noqa: E402
from mmrisk.streaming.events import split_stream  # noqa: E402
from mmrisk.streaming.store import read  # noqa: E402

CLOCK = lambda: datetime(2026, 3, 1, 12, 0, 0)  # noqa: E731
LIMIT = 600


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("stream")
    src, lake = root / "src", root / "lake"
    generate(
        GeneratorConfig(
            num_customers=400, num_transactions=24_000, fraud_rate=0.02, seed=31,
            start_date=date(2025, 1, 1), end_date=date(2025, 12, 31), output_dir=src,
        )
    )  # fmt: skip
    ingest_source_dir(src, lake, CLOCK)
    return root, lake


@pytest.fixture(scope="module")
def data(env):
    return {t: read_layer(env[1], "cleaned", t) for t in ("transactions", "customers", "labels")}


@pytest.fixture(scope="module")
def model(data):
    features = compute_features(data["transactions"], data["customers"])
    split = make_split(features, data["labels"])
    booster, _ = fit_lightgbm(split, seed=0, quick=True)
    cal = PlattCalibrator.fit(booster.score(split.val), split.val["label_is_fraud"].to_numpy())
    return RiskModel(booster, cal, CostModel(review_cost=2.0))  # cheap reviews: more flagged


@pytest.fixture(scope="module")
def model_dir(env, model):
    model.save(env[0] / "advanced")
    return env[0] / "advanced"


@pytest.fixture(scope="module")
def parts_(data):
    """(history, the first LIMIT stream events' rows, start)."""
    history, stream, start = split_stream(data["transactions"], last_days=10)
    return history, stream.head(LIMIT), start


@pytest.fixture(scope="module")
def batch(model, data, parts_):
    ids = parts_[1]["transaction_id"].to_list()
    feats = compute_features(data["transactions"], data["customers"])
    out = model.assess(feats.filter(pl.col("transaction_id").is_in(ids)))
    return {r["transaction_id"]: r for r in out.to_dicts()}


def run(model, data, parts_, tmp_path, faults=None, lateness=60.0, store=True, scorer=None):
    history, stream, _ = parts_
    scorer = scorer or LocalScorer(ScoringService(model, data["customers"], history))
    cfg = StreamConfig(max_lateness=lateness, faults=faults or FaultConfig())
    st = PredictionStore(tmp_path / "p") if store else None
    return run_stream(stream.to_dicts(), scorer, cfg, st), tmp_path / "p"


def assert_same_as_batch(path, batch, expect):
    df = read(path)
    assert df.height == expect and set(df["transaction_id"]) <= set(batch)
    for r in df.to_dicts():
        want = batch[r["transaction_id"]]
        assert r["risk_probability"] == pytest.approx(want["risk_probability"], abs=1e-12)
        assert r["action"] == want["action"] and r["risk_level"] == want["risk_level"]


# ---- the stream scores exactly as batch scoring does ----


def test_a_clean_stream_equals_batch_scoring(model, data, parts_, batch, tmp_path):
    report, path = run(model, data, parts_, tmp_path)
    assert report["balanced"] and report["outcomes"]["scored"] == LIMIT
    assert_same_as_batch(path, batch, LIMIT)
    actions = {r["action"] for r in read(path).to_dicts()}
    assert len(actions) >= 2  # flagged and allowed transactions were both compared


def test_a_messy_stream_still_equals_batch_scoring(model, data, parts_, batch, tmp_path):
    faults = FaultConfig(duplicate_rate=0.05, bad_rate=0.03, jitter_seconds=600, seed=5)
    report, path = run(model, data, parts_, tmp_path, faults, lateness=600)
    ev = report["events"]["by_kind"]
    assert ev["duplicate"] > 10 and ev["invalid"] > 5  # the faults happened
    assert report["outcomes"]["scored"] == LIMIT and report["buffer"]["dropped_late"] == 0
    assert report["outcomes"]["invalid"] == ev["invalid"]
    assert report["buffer"]["dropped_duplicate"] == ev["duplicate"]
    assert report["buffer"]["max_waiting"] > 1 and report["balanced"]
    assert_same_as_batch(path, batch, LIMIT)  # jitter was undone by the buffer


def test_without_the_reordering_window_the_same_stream_loses_events(model, data, parts_, tmp_path):
    faults = FaultConfig(jitter_seconds=600, seed=5)
    report, _ = run(model, data, parts_, tmp_path, faults, lateness=0)
    lost = report["buffer"]["dropped_late"] + report["outcomes"]["out_of_order"]
    assert lost > 0 and report["outcomes"]["scored"] < LIMIT and report["balanced"]


def test_the_same_seed_gives_the_same_predictions(model, data, parts_, tmp_path):
    faults = FaultConfig(duplicate_rate=0.05, bad_rate=0.03, jitter_seconds=120, seed=9)
    a, pa = run(model, data, parts_, tmp_path / "a", faults)
    b, pb = run(model, data, parts_, tmp_path / "b", faults)
    drop = ["processed_at"]
    assert read(pa).drop(drop).equals(read(pb).drop(drop))
    assert (
        a["outcomes"] == b["outcomes"]
        and a["buffer"] == b["buffer"]
        and a["actions"] == b["actions"]
    )
    c, _ = run(model, data, parts_, tmp_path / "c", FaultConfig(0.05, 0.03, 120, seed=10))
    assert c["events"]["by_kind"] != a["events"]["by_kind"]


# ---- through a running API ----


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_scoring_through_the_api_gives_the_same_predictions(model, data, parts_, batch, tmp_path):
    history, stream, _ = parts_
    app = create_app(ScoringService(model, data["customers"], history))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    try:
        scorer = HttpScorer(f"http://127.0.0.1:{port}")
        assert scorer.ready()
        faults = FaultConfig(duplicate_rate=0.05, bad_rate=0.03, jitter_seconds=60, seed=2)
        report, path = run(model, data, parts_, tmp_path, faults, scorer=scorer)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert report["scorer"] == "http" and report["outcomes"]["scored"] == LIMIT
    assert report["outcomes"]["unavailable"] == 0 and report["balanced"]
    assert_same_as_batch(path, batch, LIMIT)


# ---- the command line ----


def base_args(env, model_dir, store, *extra):
    return ["run", "--lake", str(env[1]), "--model-dir", str(model_dir), "--last-days", "10",
            "--limit", "300", "--store", str(store), *extra]  # fmt: skip


def test_run_then_summary_then_append(env, model_dir, tmp_path, capsys):
    store = tmp_path / "p"
    assert cli(base_args(env, model_dir, store, "--duplicate-rate", "0.02",
                         "--jitter-seconds", "30")) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "scored 300" in out and "every event is accounted for" in out
    report = json.loads((store / "stream_run.json").read_text())
    assert report["balanced"] and report["outcomes"]["scored"] == 300
    assert cli(["summary", "--store", str(store)]) == 0
    assert "300 predictions" in capsys.readouterr().out
    assert cli(base_args(env, model_dir, store)) == 2  # would mix two runs of the same events
    assert "--append" in capsys.readouterr().err
    assert cli(base_args(env, model_dir, store, "--append", "--from", "2026-01-01")) == 2
    assert "no transactions" in capsys.readouterr().err


def test_no_store_leaves_nothing_behind(env, model_dir, tmp_path):
    store = tmp_path / "none"
    assert cli(base_args(env, model_dir, store, "--no-store")) == 0
    assert not store.exists()


def test_user_mistakes_exit_with_code_2(env, model_dir, tmp_path, capsys):
    s = str(tmp_path / "x")
    assert cli(["run", "--lake", str(tmp_path / "none"), "--store", s]) == 2
    assert cli(["run", "--lake", str(env[1]), "--model-dir", str(tmp_path), "--store", s]) == 2
    assert cli(base_args(env, model_dir, s, "--duplicate-rate", "2")) == 2
    assert cli(base_args(env, model_dir, s, "--speed", "-1")) == 2
    assert cli(base_args(env, model_dir, s, "--url", "http://127.0.0.1:9")) == 2  # needs --from
    err = capsys.readouterr().err
    assert "--from is required" in err and err.count("error:") == 5
    url_args = base_args(env, model_dir, s, "--url", f"http://127.0.0.1:{free_port()}")
    assert cli([*url_args, "--from", "2025-12-01"]) == 2  # nothing is listening
    assert "not ready" in capsys.readouterr().err
    assert not (tmp_path / "x").exists() or not PredictionStore.has_parts(tmp_path / "x")


def test_summary_of_an_empty_folder(tmp_path, capsys):
    assert cli(["summary", "--store", str(tmp_path)]) == 0
    assert "no predictions" in capsys.readouterr().out


def test_a_slow_paced_run_takes_about_as_long_as_the_speed_says(model, data, parts_, tmp_path):
    history, stream, _ = parts_
    few = stream.head(20)
    span = (few["timestamp"].max() - few["timestamp"].min()).total_seconds()
    scorer = LocalScorer(ScoringService(model, data["customers"], history))
    assert span > 60  # the events really are minutes apart in simulated time
    speed = span / 0.5  # half a second of real time
    t0 = time.perf_counter()
    report = run_stream(few.to_dicts(), scorer, StreamConfig(speed=speed, max_lateness=0))
    took = time.perf_counter() - t0
    assert 0.45 < took < 3 and report["outcomes"]["scored"] == 20


# ---- Phase 14: the metrics and the monitoring checks on a real stream ----


def fetch(url):
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310
        return r.read().decode()


def series_value(text, series):
    for line in text.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{series} not exposed")


def test_the_api_metrics_agree_with_what_a_stream_did(model, data, parts_, tmp_path):
    history, stream, _ = parts_
    app = create_app(ScoringService(model, data["customers"], history))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    try:
        url = f"http://127.0.0.1:{port}"
        scorer = HttpScorer(url)
        assert scorer.ready()
        faults = FaultConfig(duplicate_rate=0.05, bad_rate=0.03, jitter_seconds=60, seed=2)
        report, path = run(model, data, parts_, tmp_path, faults, scorer=scorer)
        again = scorer.score(stream.to_dicts()[0])  # a repeat sent straight to the API
        text = fetch(url + "/metrics")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    stored = read(path)
    assert again.status == "duplicate" and report["outcomes"]["invalid"] > 0
    for action in ("ALLOW", "REVIEW", "BLOCK"):
        counted = series_value(text, f'mmrisk_transactions_scored_total{{action="{action}"}}')
        assert counted == (stored["action"] == action).sum(), action
    assert series_value(text, "mmrisk_risk_probability_count") == LIMIT
    assert series_value(text, "mmrisk_duplicate_requests_total") == 1
    bad = 'mmrisk_http_requests_total{endpoint="/v1/score",method="POST",status="422"}'
    assert series_value(text, bad) == report["outcomes"]["invalid"]
    assert series_value(text, "mmrisk_history_transactions") == history.height + LIMIT
    assert series_value(text, "mmrisk_service_ready") == 1
    # the probability histogram's sum is the sum of the stored probabilities
    assert series_value(text, "mmrisk_risk_probability_sum") == pytest.approx(
        stored["risk_probability"].sum(), rel=1e-9
    )
    for private in (stream["customer_id"][0], stream["transaction_id"][0]):
        assert private not in text


def test_monitoring_accepts_a_healthy_stream_and_catches_a_changed_decision_rule(
    model, data, parts_, tmp_path, capsys
):
    from mmrisk.monitoring.cli import main as monitor

    _, stream, _ = parts_
    healthy, hp = run(model, data, parts_, tmp_path / "healthy")
    # the same model with expensive reviews flags far fewer transactions
    costly = RiskModel(model.scorer, model.calibrator, CostModel(review_cost=2000.0))
    changed, cp = run(costly, data, parts_, tmp_path / "changed")
    n_healthy = read(hp)["action"].ne("ALLOW").sum()
    n_changed = read(cp)["action"].ne("ALLOW").sum()
    assert n_healthy >= 20 and n_changed < n_healthy * 0.5  # the setting really changed decisions
    out = tmp_path / "mon"
    assert monitor(["baseline", "--store", str(hp), "--out", str(out), "--window", "6h"]) == 0
    base = str(out / "baseline.json")
    ok = ["report", "--store", str(hp), "--baseline", base, "--out", str(out), "--window", "6h"]
    assert monitor(ok) == 0
    capsys.readouterr()
    bad = [
        "report",
        "--store",
        str(cp),
        "--baseline",
        base,
        "--out",
        str(out / "x"),
        "--window",
        "6h",
    ]
    code = monitor(bad)
    printed = capsys.readouterr().out
    assert code == 1 and "FAIL  flagged share like the baseline" in printed
    assert (out / "x" / "report.md").exists() and (out / "x" / "report.json").exists()
    assert stream.height == LIMIT and healthy["balanced"] and changed["balanced"]


# ---- Phase 15: drift detection on a real lake and model ----

REF_FROM, REF_TO = "2025-04-01T00:00:00", "2025-07-01T00:00:00"


def drift_cli(*args):
    from mmrisk.drift.cli import main as drift

    return drift([str(a) for a in args])


@pytest.fixture(scope="module")
def drift_ref(env, model_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("drift") / "reference.json"
    code = drift_cli(
        "reference", "--lake", env[1], "--model-dir", model_dir,
        "--from", REF_FROM, "--to", REF_TO, "--out", out,
    )  # fmt: skip
    assert code == 0
    return out


def drift_report(env, model_dir, ref, out, *extra):
    code = drift_cli("report", "--lake", env[1], "--model-dir", model_dir, "--reference", ref,
                     "--out", out, *extra)  # fmt: skip
    return code, json.loads((out / "report.json").read_text()) if (
        out / "report.json"
    ).exists() else None


def psi_of(payload, name):
    return {r["name"]: r for r in payload["results"]}[name]


def test_a_later_healthy_window_shows_no_drift(env, model_dir, drift_ref, tmp_path):
    code, p = drift_report(env, model_dir, drift_ref, tmp_path, "--last-days", "30")
    assert code == 0 and p["drift_detected"] is False
    assert p["rows"] > 500 and p["injected"] is None
    for r in p["results"]:
        if r["status"] == "FAIL":  # only a feature that is expected to move may be MAJOR
            assert r["policy"] == "accumulating", r
    assert psi_of(p, "is_salary_window")["status"] == "SKIPPED"  # 30 days is under a month
    assert (tmp_path / "report.md").read_text().startswith("# Drift report")
    assert sorted(x.name for x in tmp_path.iterdir()) == ["report.json", "report.md"]


def test_scaled_amounts_are_found_and_named(env, model_dir, drift_ref, tmp_path):
    code, p = drift_report(env, model_dir, drift_ref, tmp_path, "--last-days", "30",
                           "--scale-amounts", "4")  # fmt: skip
    assert code == 1 and p["drift_detected"] is True and "amount" in p["verdict"]
    assert (
        psi_of(p, "amount_kes")["status"] == "FAIL" and psi_of(p, "amount_log")["status"] == "FAIL"
    )
    assert psi_of(p, "channel")["status"] == "PASS"  # what did not change is not blamed
    assert p["injected"] and "Injected shift" in (tmp_path / "report.md").read_text()


def test_a_scale_of_one_changes_nothing(env, model_dir, drift_ref, tmp_path):
    _, plain = drift_report(env, model_dir, drift_ref, tmp_path / "a", "--last-days", "30")
    _, one = drift_report(env, model_dir, drift_ref, tmp_path / "b", "--last-days", "30",
                          "--scale-amounts", "1")  # fmt: skip
    for a, b in zip(plain["results"], one["results"], strict=True):
        assert a["psi"] == b["psi"] and a["status"] == b["status"]


def test_the_reference_compared_with_its_own_window_shows_no_difference(
    env, model_dir, drift_ref, tmp_path
):
    code, p = drift_report(env, model_dir, drift_ref, tmp_path, "--from", REF_FROM, "--to", REF_TO)
    assert code == 0 and p["days"] == pytest.approx(91.0)
    scored = [r for r in p["results"] if r["psi"] is not None]
    assert len(scored) == 29 and all(
        abs(r["psi"]) < 1e-9 for r in scored
    )  # 28 features + the score


def test_a_replaced_model_shows_as_a_change_in_the_score(
    env, model, model_dir, drift_ref, tmp_path
):
    other = RiskModel(model.scorer, PlattCalibrator(1.0, 3.0, "logit"), CostModel(review_cost=2.0))
    other.save(tmp_path / "other")
    code, p = drift_report(
        env, tmp_path / "other", drift_ref, tmp_path / "out", "--last-days", "30"
    )
    assert code == 1 and psi_of(p, "risk_probability")["status"] == "FAIL"
    assert p["verdict"].startswith("DRIFT DETECTED") and "risk_probability" in p["verdict"]
    for r in p["results"]:
        if r["policy"] == "monitored":
            assert r["status"] != "FAIL", r  # the inputs did not move; only the model did


def test_the_validation_window_comes_from_the_model_when_it_is_saved_there(
    env, model_dir, tmp_path
):
    meta = json.loads((model_dir / "risk_model.json").read_text())
    assert "split" not in meta  # a test model has none: the reference needs --from and --to
    assert (
        drift_cli(
            "reference", "--lake", env[1], "--model-dir", model_dir, "--out", tmp_path / "r.json"
        )
        == 2
    )
    import shutil

    copy = tmp_path / "model"
    shutil.copytree(model_dir, copy)
    meta["split"] = {"val_start": "2025-05-01 00:00:00", "test_start": "2025-06-01 00:00:00"}
    (copy / "risk_model.json").write_text(json.dumps(meta))
    ref = tmp_path / "r.json"
    assert drift_cli("reference", "--lake", env[1], "--model-dir", copy, "--out", ref) == 0
    assert json.loads(ref.read_text())["window"] == {
        "from": "2025-05-01T00:00:00",
        "to": "2025-06-01T00:00:00",
    }


def test_drift_user_mistakes_exit_two_with_nothing_written(
    env, model_dir, drift_ref, tmp_path, capsys
):
    ok = ["--lake", env[1], "--model-dir", model_dir]
    out = tmp_path / "o"
    bad_version = tmp_path / "v.json"
    data = json.loads(drift_ref.read_text())
    data["feature_version"] = "9.9"
    bad_version.write_text(json.dumps(data))
    rep = ["report", *ok, "--reference", drift_ref, "--out", out]
    cases = [
        (["report", *ok, "--reference", tmp_path / "none.json", "--out", out], "cannot read"),
        (["report", *ok, "--reference", bad_version, "--out", out], "feature version"),
        ([*rep, "--from", "soon"], "ISO time"),
        ([*rep, "--from", "2025-12-30", "--to", "2025-12-01"], "before"),
        ([*rep, "--scale-amounts", "0"], "above zero"),
        ([*rep, "--last-days", "0"], "above zero"),
        ([*rep, "--last-days", "1"], "at least"),
        ([*rep, "--from", "2030-01-01", "--to", "2030-02-01"], "no transactions"),
        (
            [
                "reference",
                *ok,
                "--from",
                "2025-06-01",
                "--to",
                "2025-05-01",
                "--out",
                out / "r.json",
            ],
            "before",
        ),
        (
            [
                "reference",
                "--lake",
                tmp_path / "nolake",
                "--model-dir",
                model_dir,
                "--out",
                out / "r.json",
            ],
            "not found",
        ),
    ]
    for args, word in cases:
        assert drift_cli(*args) == 2, args
        assert word in capsys.readouterr().err, args
    assert not out.exists()
