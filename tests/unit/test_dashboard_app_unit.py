"""The Streamlit page, driven by Streamlit's own test harness (no browser)."""

import json
from pathlib import Path

import pytest
from dashboard_helpers import drift_payload, make_registry, make_store

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parents[2] / "src" / "mmrisk" / "dashboard" / "app.py"
ENV = ("MMRISK_DASH_STORE MMRISK_DASH_BASELINE MMRISK_DASH_DRIFT MMRISK_DASH_REGISTRY "
       "MMRISK_DASH_RETRAIN").split()  # fmt: skip


def launch(monkeypatch, tmp_path, store=True, extras=True, n=600):
    paths = {k: tmp_path / k.lower() for k in ENV}
    if store:
        make_store(paths["MMRISK_DASH_STORE"], n=n)
    if extras:
        make_registry(paths["MMRISK_DASH_REGISTRY"], tmp_path)
        paths["MMRISK_DASH_DRIFT"].write_text(json.dumps(drift_payload()))
        rt = paths["MMRISK_DASH_RETRAIN"]
        rt.mkdir()
        (rt / "check.json").write_text(json.dumps({"retrain": True, "triggers": [
            {"name": "drift", "fired": True, "detail": "d"}]}))  # fmt: skip
    for k, v in paths.items():
        monkeypatch.setenv(k, str(v))
    return AppTest.from_file(str(APP), default_timeout=60).run()


def text(at):
    return " ".join(
        [m.value for m in at.markdown] + [c.value for c in at.caption]
        + [i.value for i in at.info] + [e.value for e in at.error]
        + [s.value for s in at.success] + [w.value for w in at.warning]
    )  # fmt: skip


def test_page_renders_every_tab_with_data(monkeypatch, tmp_path):
    at = launch(monkeypatch, tmp_path)
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Overview", "Review queue", "Health", "Models"]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Transactions scored"] == "600"
    assert metrics["Flagged (review + block)"] == "6.00%"
    assert "Synthetic data only" in text(at)
    assert "Production: v0001" in text(at)
    assert "Prediction-store checks:" in text(at)


def test_page_with_nothing_present_explains_what_to_run(monkeypatch, tmp_path):
    at = launch(monkeypatch, tmp_path, store=False, extras=False)
    assert not at.exception and not at.error
    body = text(at)
    assert "no predictions" in body and "streaming run" in body
    assert "no model registry" in body and "no retraining results" in body
    assert not at.metric


def test_damaged_store_is_an_error_message_not_a_crash(monkeypatch, tmp_path):
    paths = {k: tmp_path / k.lower() for k in ENV}
    store = make_store(paths["MMRISK_DASH_STORE"], n=30)
    (store / "part-000001.parquet").write_bytes(b"garbage")
    for k, v in paths.items():
        monkeypatch.setenv(k, str(v))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert any("cannot read the predictions" in e.value for e in at.error)


def test_customer_ids_are_masked_until_the_box_is_unticked(monkeypatch, tmp_path):
    at = launch(monkeypatch, tmp_path)
    queue = next(d for d in at.dataframe if "customer" in list(d.value.columns))
    assert all(str(c).endswith("***") for c in queue.value["customer"])
    box = next(c for c in at.checkbox if c.label == "Mask customer ids")
    assert box.value is True
    box.uncheck().run()
    queue = next(d for d in at.dataframe if "customer" in list(d.value.columns))
    assert all("*" not in str(c) for c in queue.value["customer"])


def test_a_reason_with_markup_is_shown_as_text(monkeypatch, tmp_path):
    from mmrisk.streaming.store import PredictionStore

    paths = {k: tmp_path / k.lower() for k in ENV}
    make_store(paths["MMRISK_DASH_STORE"], n=60)
    evil = "<script>alert(1)</script> **bold**"
    store = PredictionStore(paths["MMRISK_DASH_STORE"])
    from dashboard_helpers import START, reasons

    store.add(
        10_000,
        {"transaction_id": "TEVIL", "customer_id": "C999999", "timestamp": START,
         "transaction_type": "send_money", "amount_kes": 1, "channel": "app",
         "location_county": "Nairobi"},
        {"risk_probability": 0.99, "action": "BLOCK", "risk_level": "HIGH",
         "reasons": reasons(evil), "feature_version": "1.0"},
    )  # fmt: skip
    store.close()
    for k, v in paths.items():
        monkeypatch.setenv(k, str(v))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    queue = next(d for d in at.dataframe if "top_reason" in list(d.value.columns))
    assert queue.value["top_reason"][0] == evil  # highest probability first, carried verbatim
    assert not any("<script>" in m.value for m in at.markdown)  # never rendered as HTML


def test_page_source_never_renders_html_or_writes_files():
    src = APP.read_text(encoding="utf-8")
    assert "unsafe_allow_html" not in src and "unsafe_allow_javascript" not in src
    for forbidden in ("write_text", "write_parquet", "open(", "subprocess", "requests", "httpx"):
        assert forbidden not in src, forbidden


def test_filters_and_window_change_do_not_break_the_page(monkeypatch, tmp_path):
    at = launch(monkeypatch, tmp_path)
    next(s for s in at.selectbox if s.label == "Window").select("1h").run()
    assert not at.exception
    at.multiselect[0].set_value(["BLOCK"]).run()
    assert not at.exception
    queue = next(d for d in at.dataframe if "top_reason" in list(d.value.columns))
    assert set(queue.value["action"]) == {"BLOCK"}
    at.multiselect[0].set_value([]).run()  # no action chosen: an empty, friendly result
    assert not at.exception and "No transactions match" in text(at)


def test_the_detail_view_masks_the_customer_too(monkeypatch, tmp_path):
    at = launch(monkeypatch, tmp_path)
    detail = json.loads(at.json[0].value)
    assert detail["customer"].endswith("***")
    next(c for c in at.checkbox if c.label == "Mask customer ids").uncheck().run()
    assert "*" not in json.loads(at.json[0].value)["customer"]
