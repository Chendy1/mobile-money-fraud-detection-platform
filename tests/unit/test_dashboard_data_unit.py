"""The dashboard's data layer: what each page would show, from files, without Streamlit."""

import json
from datetime import timedelta

import polars as pl
import pytest
from dashboard_helpers import START, drift_payload, make_registry, make_store

from mmrisk.dashboard import data as D
from mmrisk.streaming import store as pstore


@pytest.fixture(scope="module")
def df(tmp_path_factory):
    return pstore.read(make_store(tmp_path_factory.mktemp("store")))


# ---- the store --------------------------------------------------------------------------------


def test_load_store_states(tmp_path):
    missing = D.load_store(tmp_path / "nothing")
    assert missing.state == D.MISSING and "streaming run" in missing.hint
    path = make_store(tmp_path / "s", n=60, flush_rows=1000)
    assert D.load_store(path).ok and D.load_store(path).value.height == 60
    (path / "part-000001.parquet").write_bytes(b"not parquet")
    broken = D.load_store(path)
    assert broken.state == D.BROKEN and "cannot read" in broken.message


def test_load_store_refuses_a_store_with_missing_columns(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    pl.DataFrame({"seq": [1], "transaction_id": ["a"]}).write_parquet(d / "part-000001.parquet")
    bad = D.load_store(d)
    assert bad.state == D.BROKEN and "lack columns" in bad.message


def test_store_signature_changes_when_a_part_is_added(tmp_path):
    path = make_store(tmp_path / "s", n=100, flush_rows=1000)
    before = D.store_signature(path)
    assert before == D.store_signature(path)
    pstore.PredictionStore(path).close()  # no rows: nothing added
    assert before == D.store_signature(path)
    make_store(path, n=10)  # appends a part
    assert before != D.store_signature(path)
    assert D.store_signature(tmp_path / "none") == ()


def test_overview_numbers(df):
    ov = D.overview(df, "1d")
    assert ov["rows"] == 600
    assert ov["action_count"] == {"ALLOW": 564, "REVIEW": 24, "BLOCK": 12}
    assert ov["flagged_share"] == pytest.approx(36 / 600)
    assert sum(ov["action_share"].values()) == pytest.approx(1.0)
    assert ov["first_event"] == START and ov["last_event"] == START + timedelta(minutes=7 * 599)
    assert ov["feature_versions"] == ["1.0"]
    assert ov["windows"]["rows"].sum() == 600
    assert 0 < ov["p99_probability"] <= 1 and ov["mean_probability"] > 0
    assert D.overview(df.clear(), "1d") == {"rows": 0}


def test_histogram_covers_every_row_and_clips_one(df):
    h = D.probability_histogram(df, 20)
    assert h.height == 20 and h["rows"].sum() == 600
    assert h["bin"][0] == 0.0 and h["bin"][-1] == pytest.approx(0.95)
    top = pl.DataFrame({"risk_probability": [0.0, 1.0, 0.999, 0.5]})
    h2 = D.probability_histogram(top, 10)
    assert h2["rows"][-1] == 2  # 1.0 joins the last bin
    assert h2["rows"].sum() == 4
    with pytest.raises(ValueError):
        D.probability_histogram(top, 0)


# ---- the review queue -------------------------------------------------------------------------


def test_mask_id():
    assert D.mask_id("C000123") == "C000***"
    assert D.mask_id("AB") == "A*"
    assert D.mask_id("") == "" and D.mask_id(None) == ""


def test_parse_reasons_is_forgiving():
    good = json.dumps([{"rank": 1, "text": "x"}, "junk", 3])
    assert D.parse_reasons(good) == [{"rank": 1, "text": "x"}]
    for bad in (None, "", "{not json", '{"a": 1}', "5"):
        assert D.parse_reasons(bad) == []


def test_queue_orders_by_probability_then_time_then_id(df):
    q = D.review_queue(df)
    assert q.height == 36 and set(q["action"]) == {"REVIEW", "BLOCK"}
    assert q["probability"].to_list() == sorted(q["probability"].to_list(), reverse=True)
    assert q["action"][0] == "BLOCK" and q["action"][-1] == "REVIEW"
    tie = pl.concat([df.head(3), df.head(3)]).with_columns(
        pl.lit(0.5).alias("risk_probability"), pl.lit("REVIEW").alias("action")
    )
    t = D.review_queue(tie)
    assert t["transaction_id"].to_list() == sorted(t["transaction_id"].to_list())


def test_queue_filters_and_limit(df):
    assert D.review_queue(df, actions=("BLOCK",)).height == 12
    assert D.review_queue(df, actions=("ALLOW",), limit=5).height == 5
    assert D.review_queue(df, min_probability=0.5).height == 12
    only = D.review_queue(df, counties=["Kisumu"])
    assert set(only["county"]) == {"Kisumu"} and 0 < only.height < 36
    ussd = D.review_queue(df, channels=["ussd"])
    assert set(ussd["channel"]) == {"ussd"}
    both = D.review_queue(df, counties=["Kisumu"], channels=["ussd"])
    assert both.height <= min(only.height, ussd.height)
    assert D.review_queue(df, limit=7).height == 7


def test_queue_argument_checks(df):
    for kw in ({"limit": 0}, {"limit": D.QUEUE_LIMIT_MAX + 1}, {"min_probability": 1.5},
               {"min_probability": -0.1}, {"actions": ("MAYBE",)}):  # fmt: skip
        with pytest.raises(ValueError):
            D.review_queue(df, **kw)
    assert D.review_queue(df, limit=D.QUEUE_LIMIT_MAX).height == 36  # the edge is allowed


def test_queue_masks_ids_by_default_and_only_then(df):
    masked = D.review_queue(df)
    assert all(c.endswith("***") for c in masked["customer"])
    open_ = D.review_queue(df, mask=False)
    assert all(c.startswith("C0000") and "*" not in c for c in open_["customer"])


def test_queue_shows_the_top_reason(df):
    q = D.review_queue(df)
    assert all(t.startswith("large amount") for t in q["top_reason"])
    assert D.review_queue(df, actions=("ALLOW",), limit=3)["top_reason"].to_list() == ["", "", ""]


def test_transaction_detail(df):
    d = D.transaction_detail(df, "T00019")
    assert d["action"] == "REVIEW" and d["customer"].endswith("***")
    assert d["reasons"][0]["feature"] == "amount_kes" and len(d["reasons"]) == 2
    assert D.transaction_detail(df, "T00019", mask=False)["customer"] == "C000019"
    assert D.transaction_detail(df, "nope") is None


# ---- health and drift -------------------------------------------------------------------------


def test_health_without_a_baseline_says_so(df):
    h = D.health(df, None, "1d")
    assert h.ok and h.value["overall"] in ("PASS", "WARN") and "no baseline" in h.value["note"]
    assert any(c.name == "predictions exist" for c in h.value["checks"])
    assert D.health(df, None, "1d", review_capacity=0).state == D.BROKEN


def test_health_with_a_baseline_and_its_failures(df, tmp_path):
    from mmrisk.monitoring.checks import profile

    good = tmp_path / "b.json"
    good.write_text(json.dumps(profile(df)))
    assert D.health(df, good, "1d").value["note"] == ""
    for body in ("{broken", "[]", '{"flagged_share": 1}',
                 '{"flagged_share":1,"rows_per_hour":1,"action_share":5}'):  # fmt: skip
        bad = tmp_path / "bad.json"
        bad.write_text(body)
        assert D.health(df, bad, "1d").state == D.BROKEN, body
    assert D.health(df, tmp_path / "absent.json", "1d").value[
        "note"
    ]  # a missing file = no baseline
    assert D.health(df.clear(), None, "1d").value["overall"] == "FAIL"
    assert D.health(df, None, "2x").state == D.BROKEN


def test_drift_summary(tmp_path):
    p = tmp_path / "r.json"
    assert D.drift_summary(p).state == D.MISSING
    p.write_text(json.dumps(drift_payload(n=12)))
    d = D.drift_summary(p).value
    assert d["top"].height == D.DRIFT_TOP and d["detected"] is False
    assert d["top"]["psi"].to_list() == sorted(d["top"]["psi"].to_list(), reverse=True)
    assert d["top"]["feature"][0] == "f11" and "skipped_one" not in d["top"]["feature"].to_list()
    assert d["injected"] is None and d["window"] == "w1"
    p.write_text(json.dumps(drift_payload(injected="amounts x3")))
    assert D.drift_summary(p).value["injected"] == "amounts x3"
    for body in ("nope", "[]", '{"drift_detected": true}'):
        p.write_text(body)
        assert D.drift_summary(p).state == D.BROKEN, body


# ---- models and retraining --------------------------------------------------------------------


def test_models_view(tmp_path):
    assert D.models_view(tmp_path / "none").state == D.MISSING
    (tmp_path / "empty").mkdir()
    assert D.models_view(tmp_path / "empty").state == D.MISSING
    reg = make_registry(tmp_path / "reg", tmp_path)
    m = D.models_view(reg.root).value
    assert m.versions["version"].to_list() == ["v0001", "v0002", "v0003"]
    assert m.versions.filter(pl.col("production"))["version"].to_list() == ["v0001"]
    assert m.production["version"] == "v0001"
    assert [(h["action"], h["version"]) for h in m.history] == [  # newest first
        ("register", "v0003"),
        ("register", "v0002"),
        ("promote", "v0001"),
        ("register", "v0001"),
    ]
    assert D.gates_table(m.details["v0001"]).is_empty()
    g = D.gates_table(m.details["v0003"])
    assert g["result"].to_list() == ["PASS", "FAIL", "WARN"]
    assert g["kind"].to_list() == ["required", "required", "advisory"]
    (reg.root / "production.json").write_text("{bad")
    assert D.models_view(reg.root).state == D.BROKEN


def test_models_history_is_limited(tmp_path):
    reg = make_registry(tmp_path / "reg", tmp_path)
    for _ in range(D.HISTORY_ROWS):
        reg.set_stage("v0002", "approved")
    assert len(D.models_view(reg.root).value.history) == D.HISTORY_ROWS


def test_retrain_summary(tmp_path):
    assert D.retrain_summary(tmp_path / "x").state == D.MISSING
    d = tmp_path / "rt"
    d.mkdir()
    (d / "check.json").write_text(json.dumps({"retrain": False, "triggers": []}))
    r = D.retrain_summary(d).value
    assert r["check"]["retrain"] is False and r["run"] is None
    (d / "check.json").write_text("{bad")
    assert D.retrain_summary(d).state == D.BROKEN
    (d / "report.json").write_text(json.dumps({"approved": True}))
    assert D.retrain_summary(d).ok  # one good file is enough to show


def test_source_status(tmp_path):
    src = D.Sources(store=tmp_path / "p", monitoring_baseline=tmp_path / "b.json",
                    drift_report=tmp_path / "d.json", registry=tmp_path / "r",
                    retrain_dir=tmp_path / "t")  # fmt: skip
    assert [r["found"] for r in D.source_status(src)] == [False] * 5
    make_store(src.store, n=10)
    src.monitoring_baseline.write_text("{}")
    make_registry(src.registry, tmp_path)
    src.retrain_dir.mkdir()
    (src.retrain_dir / "check.json").write_text("{}")
    assert [r["found"] for r in D.source_status(src)] == [True, True, False, True, True]
    assert all("\\" not in r["path"] for r in D.source_status(src))


def test_minimum_probability_includes_the_boundary(df):
    exact = float(df.filter(pl.col("transaction_id") == "T00019")["risk_probability"][0])
    q = D.review_queue(df, min_probability=exact)
    assert "T00019" in q["transaction_id"].to_list()
    assert q["probability"].min() >= exact


def test_a_registry_folder_without_a_production_pointer_is_not_found(tmp_path):
    (tmp_path / "reg" / "versions").mkdir(parents=True)
    src = D.Sources(registry=tmp_path / "reg", store=tmp_path / "p")
    assert not next(r for r in D.source_status(src) if r["source"] == "model registry")["found"]


def test_signature_changes_when_a_part_is_replaced_by_one_of_the_same_name(tmp_path):
    path = make_store(tmp_path / "s", n=50, flush_rows=1000)
    before = D.store_signature(path)
    part = path / "part-000001.parquet"
    pl.read_parquet(part).head(10).write_parquet(part)
    assert D.store_signature(path) != before
