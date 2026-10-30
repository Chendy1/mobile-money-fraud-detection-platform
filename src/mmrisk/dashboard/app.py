"""The Streamlit page. Thin on purpose: everything it shows comes from `mmrisk.dashboard.data`.

Run it with `python -m mmrisk.dashboard run`. Paths come from environment variables that the
command sets (MMRISK_DASH_*), because Streamlit runs this file as a script.

Safety rules kept here: no HTML from data is ever rendered, customer ids are
masked unless the person switches the mask off, and nothing is written anywhere.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import polars as pl
import streamlit as st

from mmrisk.dashboard import data as D

ENV = {
    "store": "MMRISK_DASH_STORE",
    "monitoring_baseline": "MMRISK_DASH_BASELINE",
    "drift_report": "MMRISK_DASH_DRIFT",
    "registry": "MMRISK_DASH_REGISTRY",
    "retrain_dir": "MMRISK_DASH_RETRAIN",
}


def sources_from_env() -> D.Sources:
    base = D.Sources()
    return D.Sources(**{k: Path(os.environ.get(v, str(getattr(base, k)))) for k, v in ENV.items()})


@st.cache_data(show_spinner=False)
def _store(path: str, signature: tuple) -> D.Loaded:  # the signature makes new parts reload
    return D.load_store(Path(path))


def _problem(loaded: D.Loaded) -> None:
    if loaded.state == D.BROKEN:
        st.error(loaded.message)
    else:
        st.info(f"{loaded.message}. {loaded.hint}")


def _pct(x: float) -> str:
    return f"{x:.2%}"


def overview_tab(df: pl.DataFrame, window: str) -> None:
    ov = D.overview(df, window)
    c = st.columns(4)
    c[0].metric("Transactions scored", f"{ov['rows']:,}")
    c[1].metric("Flagged (review + block)", _pct(ov["flagged_share"]))
    c[2].metric("Mean probability", f"{ov['mean_probability']:.4f}")
    c[3].metric("99th percentile probability", f"{ov['p99_probability']:.3f}")
    st.caption(
        f"Events from {ov['first_event']} to {ov['last_event']} (simulated time). "
        f"Feature version: {', '.join(ov['feature_versions'])}."
    )
    left, right = st.columns(2)
    with left:
        st.subheader("Actions")
        st.bar_chart(
            pl.DataFrame({"action": list(D.ACTIONS), "count": list(ov["action_count"].values())}),
            x="action", y="count",
        )  # fmt: skip
    with right:
        st.subheader("Probability distribution")
        st.bar_chart(D.probability_histogram(df), x="bin", y="rows")
    st.subheader(f"Per {window} window")
    table = ov["windows"]
    st.line_chart(table, x="start", y=["rows"])
    st.line_chart(table, x="start", y=["flagged_share"])
    st.caption(
        "Fraud arrives in clusters, so a single window can swing without anything being wrong."
    )


def queue_tab(df: pl.DataFrame, mask: bool) -> None:
    counties = sorted(df["location_county"].drop_nulls().unique().to_list())
    channels = sorted(df["channel"].drop_nulls().unique().to_list())
    f = st.columns(5)
    actions = f[0].multiselect("Action", ["REVIEW", "BLOCK", "ALLOW"], ["REVIEW", "BLOCK"])
    min_p = f[1].slider("Minimum probability", 0.0, 1.0, 0.0, 0.01)
    county = f[2].multiselect("County", counties)
    channel = f[3].multiselect("Channel", channels)
    limit = f[4].number_input("Rows", 1, D.QUEUE_LIMIT_MAX, 100)
    queue = D.review_queue(
        df, actions=tuple(actions), min_probability=min_p, counties=county or None,
        channels=channel or None, limit=int(limit), mask=mask,
    )  # fmt: skip
    st.caption(f"{queue.height} transactions shown, highest probability first.")
    if queue.is_empty():
        st.info("No transactions match these filters.")
        return
    st.dataframe(queue, hide_index=True)
    chosen = st.selectbox("Look at one transaction", queue["transaction_id"].to_list())
    detail = D.transaction_detail(df, chosen, mask)
    if detail is None:
        return
    reasons = detail.pop("reasons")
    st.json(detail | {"event_time": str(detail["event_time"])})
    if reasons:
        st.subheader("Reason codes")
        st.caption("What raised the risk in the model's own terms; not a cause.")
        st.dataframe(pl.DataFrame(reasons), hide_index=True)
    else:
        st.caption("No reason codes were stored for this transaction.")


def health_tab(df: pl.DataFrame, src: D.Sources, window: str, capacity: float | None) -> None:
    last = df["event_time"].max()
    as_of = datetime.combine(last.date(), last.time()) if last else None
    h = D.health(df, src.monitoring_baseline, window, as_of, capacity)
    if not h.ok:
        _problem(h)
    else:
        v = h.value
        {"PASS": st.success, "WARN": st.warning, "FAIL": st.error}[v["overall"]](
            f"Prediction-store checks: {v['overall']}"
        )
        if v["note"]:
            st.caption(v["note"])
        st.dataframe(
            pl.DataFrame([c.__dict__ for c in v["checks"]]), hide_index=True,
        )  # fmt: skip
        st.caption(
            "Freshness is measured against the newest event, not the clock, because the stream "
            "replays old data. All thresholds are assumptions."
        )
    st.subheader("Drift")
    d = D.drift_summary(src.drift_report)
    if not d.ok:
        _problem(d)
        return
    v = d.value
    if v["injected"]:
        st.warning(f"This report is a demonstration: {v['injected']}.")
    (st.error if v["detected"] else st.success)(v["verdict"] or "no verdict")
    st.caption(f"Window {v['window']} against {v['reference_window']} ({v['rows']} rows).")
    st.dataframe(v["top"], hide_index=True)
    st.caption("Population stability index: under 0.1 stable, from 0.25 major (conventions).")


def models_tab(src: D.Sources) -> None:
    m = D.models_view(src.registry)
    if not m.ok:
        _problem(m)
    else:
        v: D.ModelsView = m.value
        if v.production:
            st.success(f"Production: {v.production['version']} since {v.production['promoted_at']}")
            st.caption(f"Reason: {v.production['reason']}")
        st.dataframe(v.versions, hide_index=True)
        pick = st.selectbox("Gate results of version", v.versions["version"].to_list())
        gates = D.gates_table(v.details[pick])
        if gates.is_empty():
            st.caption("No gates were recorded for this version (the first model is not gated).")
        else:
            st.dataframe(gates, hide_index=True)
        st.subheader("Recent changes")
        if v.history:
            st.dataframe(pl.DataFrame(v.history, infer_schema_length=None), hide_index=True)
    st.subheader("Retraining")
    r = D.retrain_summary(src.retrain_dir)
    if not r.ok:
        _problem(r)
        return
    check, run = r.value["check"], r.value["run"]
    if check:
        st.write("Last check:", "retrain recommended" if check["retrain"] else "no retrain needed")
        st.dataframe(pl.DataFrame(check["triggers"]), hide_index=True)
    if run:
        outcome = "approved" if run["approved"] else "rejected" if run["trained"] else "not trained"
        st.write(f"Last run: candidate {run['candidate']} {outcome}")


def main() -> None:
    st.set_page_config(page_title="Mobile money risk (synthetic)", layout="wide")
    st.title("Mobile money transaction risk")
    st.caption("Synthetic data only. Read-only view of the files the pipeline writes.")
    src = sources_from_env()
    with st.sidebar:
        st.header("View")
        window = st.selectbox("Window", D.WINDOWS, index=D.WINDOWS.index("1d"))
        mask = st.checkbox("Mask customer ids", value=True)
        capacity = st.number_input("Review capacity per hour (0 = unknown)", 0.0, 100000.0, 0.0)
        if st.button("Refresh"):
            st.cache_data.clear()
        st.header("Sources")
        st.dataframe(pl.DataFrame(D.source_status(src)), hide_index=True)
    loaded = _store(str(src.store), D.store_signature(src.store))
    tabs = st.tabs(["Overview", "Review queue", "Health", "Models"])
    with tabs[0]:
        _problem(loaded) if not loaded.ok else overview_tab(loaded.value, window)
    with tabs[1]:
        _problem(loaded) if not loaded.ok else queue_tab(loaded.value, mask)
    with tabs[2]:
        if loaded.ok:
            health_tab(loaded.value, src, window, capacity or None)
        else:
            _problem(loaded)
    with tabs[3]:
        models_tab(src)


main()
