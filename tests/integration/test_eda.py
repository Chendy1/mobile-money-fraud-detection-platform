"""EDA tests: leakage, cross-checks against brute force, report generation, failure paths."""

import shutil
from datetime import date, datetime, timedelta

import polars as pl
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("matplotlib")

from mmrisk.eda import analyses as A  # noqa: E402
from mmrisk.eda.cli import main as eda_main  # noqa: E402
from mmrisk.eda.data import Analysis, EDAError  # noqa: E402
from mmrisk.eda.report import build_report, wilson  # noqa: E402
from mmrisk.generation import GeneratorConfig, generate  # noqa: E402
from mmrisk.ingestion import ingest_source_dir  # noqa: E402
from mmrisk.ingestion.reader import layer_files  # noqa: E402

CLOCK = lambda: datetime(2025, 7, 1, 12, 0, 0)  # noqa: E731


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("eda")
    src, lake = root / "src", root / "lake"
    generate(
        GeneratorConfig(
            num_customers=300,
            num_transactions=12_000,
            fraud_rate=0.02,
            seed=5,
            end_date=date(2025, 6, 30),
            output_dir=src,
        )
    )
    ingest_source_dir(src, lake, CLOCK)
    return src, lake


@pytest.fixture(scope="module")
def an(env):
    src, lake = env
    return Analysis.from_lake(lake, src)


def _txns(env) -> pl.DataFrame:
    return pl.concat([pl.read_parquet(f) for f in layer_files(env[1], "cleaned", "transactions")])


def test_matured_cutoff_is_45_days_before_last_transaction(an):
    assert an.cutoff - an.matured_cutoff == timedelta(days=45)


def test_windows_match_brute_force(an, env):
    """prior_n, prior_avg, n_10min, secs_since_prev recomputed with plain Polars on a sample."""
    t = _txns(env).sort("timestamp", "transaction_id")
    got = an.query("SELECT * FROM a").sort("ts", "transaction_id")
    sample_customers = t["customer_id"].unique().sort().head(12).to_list()
    for cid in sample_customers:
        mine = t.filter(pl.col("customer_id") == cid).to_dicts()
        theirs = got.filter(pl.col("customer_id") == cid).to_dicts()
        assert len(mine) == len(theirs)
        for i, (m, g) in enumerate(zip(mine, theirs, strict=True)):
            prior = mine[:i]
            assert g["prior_n"] == len(prior)
            if prior:
                avg = sum(p["amount_kes"] for p in prior) / len(prior)
                assert g["prior_avg"] == pytest.approx(avg)
                assert g["secs_since_prev"] == int(
                    (m["timestamp"] - prior[-1]["timestamp"]).total_seconds()
                )
            ts = m["timestamp"]
            n10 = sum(
                1
                for p in mine
                if ts - timedelta(minutes=10) <= p["timestamp"] <= ts - timedelta(seconds=1)
            )
            assert g["n_10min"] == n10


def test_features_do_not_see_the_future(env, tmp_path):
    """Dropping each customer's later transactions must not change earlier rows' features."""
    src, lake = env
    t = _txns(env)
    full = Analysis.from_lake(lake)
    early = tmp_path / "early"
    early.mkdir()
    # keep each customer's first 60% of rows, so every customer loses their future
    t = t.sort("timestamp", "transaction_id").with_columns(
        rank=pl.int_range(pl.len()).over("customer_id"), n=pl.len().over("customer_id")
    )
    t.filter(pl.col("rank") < pl.col("n") * 0.6).drop("rank", "n").write_parquet(
        early / "t.parquet"
    )
    files = {n: layer_files(lake, "cleaned", n) for n in ("labels", "customers")}
    trunc = Analysis.from_files(
        [early / "t.parquet"], files["labels"], files["customers"], None, None, {}
    )
    cols = [
        "cust_seq", "dev_seq", "cp_seq", "secs_since_prev", "prior_n", "prior_avg",
        "n_10min", "n_1h", "n_24h", "f_new_device", "f_new_recipient",
        "f_rapid_repeat", "f_burst", "f_location_jump",
    ]  # fmt: skip
    q = "SELECT transaction_id, " + ", ".join(cols) + " FROM a ORDER BY transaction_id"
    a = full.query(q).join(
        trunc.query("SELECT transaction_id FROM a"), on="transaction_id", how="semi"
    )
    b = trunc.query(q)
    assert a.height == b.height > 0
    assert a.equals(b)


def test_signal_table_is_consistent(an):
    sig = A.signal_table(an)
    assert set(sig["signal"]) >= {"f_night", "any_signal"}
    assert sig["precision"].is_between(0, 1).all()
    assert sig["recall"].is_between(0, 1).all()
    anyrow = sig.filter(pl.col("signal") == "any_signal").row(0, named=True)
    assert anyrow["recall"] >= sig.filter(pl.col("signal") != "any_signal")["recall"].max()


def test_class_balance_uses_only_matured_rows(an):
    q = A.label_quality(an)
    expected = an.scalar("SELECT count(*) FROM a WHERE matured")
    assert q["n"] == expected
    assert 0 < q["fraud"] < q["n"]


def test_recent_labels_are_hidden(an):
    gap = A.recent_label_gap(an)
    assert gap["known"] <= gap["eventual"]


def test_spearman_diagonal_is_one_and_symmetric(an):
    m = A.spearman_matrix(A.corr_inputs(an), list(A.CORR_COLUMNS))
    names = m["feature"].to_list()
    for i, n in enumerate(names):
        assert m[n][i] == pytest.approx(1.0)
    for i, a_ in enumerate(names):
        for j, b_ in enumerate(names):
            assert m[b_][i] == pytest.approx(m[a_][j], abs=1e-9)


def test_wilson_interval():
    lo, hi = wilson(50, 100)
    assert lo < 0.5 < hi
    assert wilson(0, 0) == (0.0, 1.0)
    assert wilson(0, 1000)[0] == 0.0


def test_report_generates_figures_and_computed_numbers(an, tmp_path):
    res = build_report(an, tmp_path / "r" / "report.md", tmp_path / "r" / "figs")
    assert all(p.exists() and p.stat().st_size > 5_000 for p in res.figure_paths)
    assert len(res.figure_paths) == 7
    assert "All data is synthetic" in res.text
    assert "Audit against ground truth" in res.text
    q = A.label_quality(an)
    assert f"{q['fraud']:,} of {q['n']:,}" in res.text
    assert "(figs/01-class-balance.png)" in res.text


def test_report_without_truth_skips_audit(env, tmp_path):
    _, lake = env
    plain = Analysis.from_lake(lake)
    assert A.truth_audit(plain) is None
    res = build_report(plain, tmp_path / "r.md", tmp_path / "f", use_truth=False)
    assert "Audit against ground truth" not in res.text


def test_empty_lake_is_a_clear_error(tmp_path):
    with pytest.raises(EDAError, match="run the ingestion"):
        Analysis.from_lake(tmp_path)


def test_cli_missing_lake_returns_2(tmp_path, capsys):
    assert eda_main(["--lake", str(tmp_path / "nope")]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_end_to_end(env, tmp_path, capsys):
    src, lake = env
    rc = eda_main(
        ["--lake", str(lake), "--source", str(src),
         "--report", str(tmp_path / "e.md"), "--figures", str(tmp_path / "f")]
    )  # fmt: skip
    assert rc == 0
    assert (tmp_path / "e.md").exists()
    assert "ground-truth audit: included" in capsys.readouterr().out
    shutil.rmtree(tmp_path / "f")


def test_cross_customer_feature_does_not_see_the_future(env, tmp_path):
    """last inbound transfer: a global time cut must not change rows before the cut."""
    _, lake = env
    t = _txns(env)
    split = t["timestamp"].sort()[int(t.height * 0.6)]
    full = Analysis.from_lake(lake)
    early = tmp_path / "early.parquet"
    t.filter(pl.col("timestamp") <= split).write_parquet(early)
    files = {n: layer_files(lake, "cleaned", n) for n in ("labels", "customers")}
    trunc = Analysis.from_files([early], files["labels"], files["customers"], None, None, {})
    q = "SELECT transaction_id, last_in_ts, f_cashout_after_inbound FROM a ORDER BY 1"
    b = trunc.query(q)
    a = full.query(q).join(b.select("transaction_id"), on="transaction_id", how="semi")
    assert a.height == b.height > 0
    assert a.equals(b)
