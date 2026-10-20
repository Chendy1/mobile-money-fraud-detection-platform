"""The streaming simulator's parts: faults, reordering, scorers, the store and the run loop."""

import json
import random
import threading
import time
from datetime import date, datetime, timedelta

import polars as pl
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("pydantic")

from mmrisk.api import ScoringService  # noqa: E402
from mmrisk.features.registry import INPUT_COLUMNS  # noqa: E402
from mmrisk.models.bundle import RiskModel  # noqa: E402
from mmrisk.models.calibration import PlattCalibrator  # noqa: E402
from mmrisk.models.costs import CostModel  # noqa: E402
from mmrisk.streaming import (  # noqa: E402
    FaultConfig,
    FaultConfigError,
    LocalScorer,
    Outcome,
    PredictionStore,
    ReorderBuffer,
    StoreError,
    StreamConfig,
    StreamError,
    iter_events,
    run_stream,
)
from mmrisk.streaming.events import split_stream  # noqa: E402
from mmrisk.streaming.scorers import (  # noqa: E402
    CONFLICT,
    DUPLICATE,
    INVALID,
    OUT_OF_ORDER,
    SCORED,
    UNAVAILABLE,
    UNKNOWN_CUSTOMER,
)
from mmrisk.streaming.store import SCHEMA, parts, read  # noqa: E402

T0 = datetime(2025, 6, 1, 12, 0, 0)


def row(i, seconds=None, cid="C000001", **kw):
    base = {
        "transaction_id": f"T{i:06d}", "customer_id": cid,
        "timestamp": T0 + timedelta(seconds=i if seconds is None else seconds),
        "transaction_type": "SEND_MONEY", "amount_kes": 1500, "counterparty_type": "CUSTOMER",
        "counterparty_id": "C000002", "device_id": "D000001", "location_county": "Nairobi",
        "channel": "APP",
    }  # fmt: skip
    return {**base, **kw}


def rows(n, step=1):
    return [row(i, i * step, cid=f"C{(i % 30) + 1:06d}") for i in range(n)]


def ids(events):
    return [e.transaction_id for e in events]


# ---- events and faults ----


def test_without_faults_the_stream_is_the_input_in_order():
    data = rows(50)
    events = list(iter_events(data))
    assert [e.txn for e in events] == data
    assert [e.seq for e in events] == list(range(50)) and {e.kind for e in events} == {"normal"}
    assert [e.arrival for e in events] == [float(i) for i in range(50)]


def test_a_stream_is_reproducible_from_its_seed():
    faults = FaultConfig(duplicate_rate=0.1, bad_rate=0.1, jitter_seconds=20, seed=7)
    a, b = list(iter_events(rows(300), faults)), list(iter_events(rows(300), faults))
    assert a == b
    other = list(iter_events(rows(300), FaultConfig(0.1, 0.1, 20, seed=8)))
    assert ids(a) != ids(other)


def test_fault_rates_are_roughly_what_was_asked_for():
    faults = FaultConfig(duplicate_rate=0.2, bad_rate=0.1, seed=3)
    events = list(iter_events(rows(5000), faults))
    kinds = [e.kind for e in events]
    assert kinds.count("normal") == 5000
    assert 0.17 < kinds.count("duplicate") / 5000 < 0.23
    assert 0.08 < kinds.count("invalid") / 5000 < 0.12
    originals = {r["transaction_id"]: r for r in rows(5000)}
    for e in events:
        if e.kind == "duplicate":
            assert e.txn == originals[e.transaction_id]
        if e.kind == "invalid":
            assert e.transaction_id.endswith("-BAD") and e.txn["amount_kes"] == 0


def test_arrivals_never_go_backwards_and_jitter_really_reorders():
    data = rows(2000)
    calm = list(iter_events(data))
    shaky = list(iter_events(data, FaultConfig(jitter_seconds=30, seed=1)))
    for events in (calm, shaky):
        assert [e.arrival for e in events] == sorted(e.arrival for e in events)

    def inversions(ev):
        return sum(a.event_time > b.event_time for a, b in zip(ev, ev[1:], strict=False))

    assert inversions(calm) == 0 and inversions(shaky) > 100  # the faults are not vacuous
    assert sorted(ids(shaky)) == sorted(ids(calm))  # nothing is lost, only moved


def test_input_must_be_in_time_order():
    with pytest.raises(ValueError, match="time order"):
        list(iter_events([row(1, 10), row(2, 5)]))


@pytest.mark.parametrize(
    "kw",
    [
        {"duplicate_rate": 1.0},
        {"bad_rate": -0.1},
        {"jitter_seconds": -1},
        {"duplicate_delay_seconds": -1},
    ],
)
def test_invalid_fault_settings_are_refused(kw):
    with pytest.raises(FaultConfigError):
        FaultConfig(**kw)


def test_split_stream_partitions_by_time_and_keeps_only_event_columns():
    lineage = pl.DataFrame(rows(100)).with_columns(_batch_id=pl.lit("abc"))
    history, stream, start = split_stream(lineage.reverse(), T0 + timedelta(seconds=60))
    assert history.height == 60 and stream.height == 40 and start == T0 + timedelta(seconds=60)
    assert history.columns == list(INPUT_COLUMNS) == stream.columns
    assert stream["timestamp"].min() >= start > history["timestamp"].max()
    _, last_week, derived = split_stream(lineage, last_days=0.0005)  # 43.2 seconds
    assert derived == lineage["timestamp"].max() - timedelta(days=0.0005)
    assert last_week.height >= 40
    with pytest.raises(ValueError):
        split_stream(lineage.head(0))


# ---- the reorder buffer ----


def drain(events, lateness):
    buf = ReorderBuffer(lateness)
    out, dropped = [], []
    for e in events:
        released, drops = buf.push(e)
        out += released
        dropped += drops
    return out + buf.flush(), dropped, buf


def in_order(events):
    keys = [(e.event_time, e.transaction_id) for e in events]
    return keys == sorted(keys)


@pytest.mark.parametrize("seed", range(5))
def test_events_delayed_by_no_more_than_the_allowed_lateness_come_out_in_order(seed):
    data = rows(1500)
    stream = list(iter_events(data, FaultConfig(jitter_seconds=25, seed=seed)))
    out, dropped, buf = drain(stream, lateness=25)
    assert dropped == [] and [e.txn for e in out] == data
    assert buf.max_waiting > 1  # events really did have to wait


@pytest.mark.parametrize("seed", range(5))
def test_whatever_arrives_the_released_stream_is_always_in_order(seed):
    stream = list(iter_events(rows(1500), FaultConfig(jitter_seconds=90, seed=seed)))
    out, dropped, _ = drain(stream, lateness=10)
    assert in_order(out)
    assert dropped and {reason for _, reason in dropped} == {"late"}
    assert len(out) + len(dropped) == len(stream)


def test_repeats_are_dropped_while_waiting_and_after_release():
    buf = ReorderBuffer(100)
    first = next(iter(iter_events([row(1, 0)])))
    assert buf.push(first)[1] == []
    assert [r for _, r in buf.push(first)[1]] == ["duplicate"]  # still waiting
    assert len(buf.flush()) == 1
    assert [r for _, r in buf.push(first)[1]] == ["duplicate"]  # already released


def test_a_repeat_of_a_forgotten_id_is_too_late_rather_than_a_new_event():
    buf = ReorderBuffer(0, remember=2)
    stream = list(iter_events([row(i, i) for i in range(1, 6)]))
    for e in stream:
        buf.push(e)
    buf.flush()
    assert [r for _, r in buf.push(stream[0])[1]] == ["late"]  # id 1 fell out of memory
    assert [r for _, r in buf.push(stream[-1])[1]] == ["duplicate"]


def test_tied_event_times_are_released_in_id_order_like_the_batch_path():
    data = [row(i, 0, cid=f"C{i + 1:06d}") for i in (3, 1, 2)]
    out, dropped, _ = drain(list(iter_events(sorted(data, key=lambda r: r["transaction_id"]))), 0)
    assert ids(out) == ["T000001", "T000002", "T000003"] and not dropped


def test_buffer_settings_are_validated():
    with pytest.raises(ValueError):
        ReorderBuffer(-1)
    with pytest.raises(ValueError):
        ReorderBuffer(1, remember=0)


# ---- scorers (in this process) ----


class Stub:
    name = "stub"

    def score(self, df):
        return df["amount_kes"].to_numpy().astype(float) / 50_000.0 - 3.0


def service(n=40):
    customers = pl.DataFrame(
        {
            "customer_id": [f"C{i:06d}" for i in range(1, n + 1)],
            "segment": ["SALARIED"] * n,
            "home_county": ["Nairobi"] * n,
            "registration_date": [date(2024, 1, 1)] * n,
        }
    )
    model = RiskModel(Stub(), PlattCalibrator(1.0, 0.0, "identity"), CostModel())
    return ScoringService(model, customers)


def test_the_local_scorer_names_every_kind_of_result():
    scorer = LocalScorer(service())
    ok = scorer.score(row(1, 10))
    assert ok.status == SCORED and ok.response["action"] in ("ALLOW", "REVIEW", "BLOCK")
    assert ok.millis >= 0
    assert scorer.score(row(1, 10)).status == DUPLICATE
    assert scorer.score(row(1, 10, amount_kes=9)).status == CONFLICT
    assert scorer.score(row(2, 5)).status == OUT_OF_ORDER
    assert scorer.score(row(3, 20, cid="C999999")).status == UNKNOWN_CUSTOMER
    assert scorer.score(row(4, 30, amount_kes=0)).status == INVALID
    assert (
        scorer.score({**row(5, 40), "_batch_id": "x"}).status == INVALID
    )  # events carry 10 columns


def test_invalid_input_never_reaches_the_service():
    svc = service()
    LocalScorer(svc).score(row(1, 10, amount_kes=-5))
    assert svc.info()["history_transactions"] == 0


# ---- the prediction store ----


def response(p=0.01, action="ALLOW", reasons=()):
    return {"risk_probability": p, "action": action, "risk_level": "LOW", "feature_version": "1.0",
            "reasons": list(reasons)}  # fmt: skip


def test_the_store_writes_complete_parts_and_reads_them_back(tmp_path):
    store = PredictionStore(tmp_path / "p", flush_rows=3)
    reasons = [{"rank": 1, "feature": "x", "effect": 0.5, "text": "t"}]
    for i in range(7):
        store.add(i, row(i, i), response(i / 10, reasons=reasons if i == 4 else ()), T0)
    assert [p.name for p in parts(tmp_path / "p")] == ["part-000001.parquet", "part-000002.parquet"]
    store.close()
    assert len(parts(tmp_path / "p")) == 3 and store.rows_written == 7
    df = read(tmp_path / "p")
    assert df.schema == pl.Schema(SCHEMA) and df["seq"].to_list() == list(range(7))
    assert df["risk_probability"].to_list() == pytest.approx([i / 10 for i in range(7)])
    assert json.loads(df.filter(pl.col("seq") == 4)["reasons"][0]) == reasons
    assert not list((tmp_path / "p").glob(".*tmp"))  # nothing half-written is left


def test_a_write_that_fails_halfway_leaves_no_part_behind(tmp_path, monkeypatch):
    store = PredictionStore(tmp_path)
    store.add(0, row(0), response(), T0)

    def broken(self, file, *args, **kwargs):
        with open(file, "wb") as handle:
            handle.write(b"PAR1 half a file")
        raise OSError("disk full")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", broken)
    with pytest.raises(OSError):
        store.flush()
    assert parts(tmp_path) == []  # a reader sees nothing rather than a broken file


def test_a_new_store_continues_the_numbering_and_ignores_unfinished_files(tmp_path):
    first = PredictionStore(tmp_path)
    first.add(0, row(0), response(), T0)
    first.close()
    (tmp_path / ".part-000002.parquet.tmp").write_bytes(b"half a file")
    again = PredictionStore(tmp_path)
    again.add(1, row(1), response(), T0)
    again.close()
    assert [p.name for p in parts(tmp_path)] == ["part-000001.parquet", "part-000002.parquet"]
    assert read(tmp_path)["seq"].to_list() == [0, 1] and PredictionStore.has_parts(tmp_path)


def test_an_empty_or_missing_store_reads_as_an_empty_table(tmp_path):
    assert read(tmp_path / "none").schema == pl.Schema(SCHEMA)
    PredictionStore(tmp_path / "empty").close()
    assert read(tmp_path / "empty").is_empty() and not PredictionStore.has_parts(tmp_path / "empty")


def test_a_file_where_a_folder_should_be_is_refused(tmp_path):
    (tmp_path / "f").write_text("x")
    with pytest.raises(StoreError):
        PredictionStore(tmp_path / "f")
    with pytest.raises(StoreError):
        PredictionStore(tmp_path / "ok", flush_rows=0)


# ---- the run loop, with a scripted scorer ----


class Script:
    """A scorer that records what it was sent and answers from a rule."""

    name = "script"

    def __init__(self, rule=None, pause=0.0):
        self.seen, self.rule, self.pause = [], rule, pause

    def score(self, txn):
        if self.pause:
            time.sleep(self.pause)
        self.seen.append(txn)
        status = self.rule(len(self.seen), txn) if self.rule else SCORED
        resp = None
        if status == SCORED:
            p = 0.9 if txn["amount_kes"] > 5000 else 0.01
            resp = response(p, "BLOCK" if p > 0.5 else "ALLOW")
        return Outcome(status, resp, 2.0 if status != UNAVAILABLE else 0.0)


def test_every_event_is_scored_once_in_order_and_accounted_for():
    data = rows(300)
    scorer = Script()
    report = run_stream(data, scorer, StreamConfig(queue_size=5))
    assert scorer.seen == data and report["balanced"]
    assert report["events"]["in"] == 300 and report["outcomes"]["scored"] == 300


def test_the_report_counts_actions_latency_and_the_window():
    data = [row(i, i, amount_kes=9000 if i % 4 == 0 else 100) for i in range(40)]
    report = run_stream(data, Script(), StreamConfig())
    assert report["actions"] == {"ALLOW": 30, "REVIEW": 0, "BLOCK": 10}
    assert report["flagged_share"] == pytest.approx(0.25)
    assert report["latency_ms"]["p50"] == 2.0 and report["latency_ms"]["max"] == 2.0
    assert report["stream_from"] == T0.isoformat()
    assert report["timing"]["simulated_seconds"] == 39.0 and report["scorer"] == "script"


def test_faults_are_counted_and_the_books_still_balance():
    faults = FaultConfig(duplicate_rate=0.2, bad_rate=0.1, jitter_seconds=5, seed=4)
    scorer = Script(lambda n, t: INVALID if t["amount_kes"] == 0 else SCORED)
    report = run_stream(rows(500), scorer, StreamConfig(max_lateness=5, faults=faults))
    ev, out, buf = report["events"], report["outcomes"], report["buffer"]
    assert ev["by_kind"]["duplicate"] > 50 and ev["by_kind"]["invalid"] > 25
    assert out["scored"] == 500 and out["invalid"] == ev["by_kind"]["invalid"]
    assert buf["dropped_duplicate"] == ev["by_kind"]["duplicate"] and buf["dropped_late"] == 0
    assert report["balanced"] and ev["in"] == sum(ev["by_kind"].values())


def test_a_slow_scorer_slows_the_producer_instead_of_growing_the_queue():
    # no reordering window, so the buffer holds nothing back and the queue is what fills up
    cfg = StreamConfig(queue_size=2, max_lateness=0)
    report = run_stream(rows(60), Script(pause=0.004), cfg)
    assert report["queue"]["max_depth"] <= 2
    assert report["queue"]["producer_blocked_seconds"] > 0.05
    assert report["outcomes"]["scored"] == 60  # slowed, not dropped


def test_a_failing_scorer_stops_the_run_without_hanging(tmp_path):
    def explode(n, txn):
        if n == 5:
            raise RuntimeError("scorer failed")
        return SCORED

    store = PredictionStore(tmp_path)
    before = threading.active_count()
    started = time.perf_counter()
    with pytest.raises(RuntimeError, match="scorer failed"):
        run_stream(rows(5000), Script(explode), StreamConfig(queue_size=2), store)
    assert time.perf_counter() - started < 10
    time.sleep(0.2)
    assert threading.active_count() <= before  # the producer thread ended
    assert read(tmp_path).height == 4  # what was scored before the failure was kept


def test_a_failing_source_is_reported():
    def source():
        yield from rows(10)
        raise OSError("disk went away")

    scorer = Script()
    with pytest.raises(StreamError, match="disk went away"):
        run_stream(source(), scorer)
    assert len(scorer.seen) == 10  # what arrived before the failure was still scored


def test_the_run_gives_up_when_the_service_stays_away():
    scorer = Script(lambda n, t: UNAVAILABLE)
    with pytest.raises(StreamError, match="in a row"):
        run_stream(rows(100), scorer, StreamConfig(max_unavailable=5))
    assert len(scorer.seen) == 5


def test_a_service_that_comes_back_does_not_end_the_run():
    scorer = Script(lambda n, t: UNAVAILABLE if n % 3 else SCORED)
    report = run_stream(rows(60), scorer, StreamConfig(max_unavailable=3))
    assert report["outcomes"]["unavailable"] == 40 and report["outcomes"]["scored"] == 20


def test_pacing_follows_the_event_times_divided_by_the_speed():
    naps, now = [], [0.0]

    def sleep(seconds):
        naps.append(seconds)
        now[0] += seconds

    run_stream(
        rows(11, step=60), Script(), StreamConfig(speed=60), clock=lambda: now[0], sleep=sleep
    )
    assert now[0] == pytest.approx(10.0)  # 600 simulated seconds at 60 per second
    assert all(n > 0 for n in naps)
    naps.clear()
    run_stream(rows(11, step=60), Script(), StreamConfig(speed=0), sleep=sleep)
    assert naps == []  # speed 0 never sleeps


def test_only_first_time_predictions_are_stored(tmp_path):
    scorer = Script(lambda n, t: DUPLICATE if n % 2 == 0 else SCORED)
    store = PredictionStore(tmp_path)
    report = run_stream(rows(10), scorer, StreamConfig(), store)
    assert report["outcomes"]["scored"] == 5 and read(tmp_path).height == 5


@pytest.mark.parametrize(
    "kw", [{"speed": -1}, {"max_lateness": -1}, {"queue_size": 0}, {"max_unavailable": 0}]
)
def test_run_settings_are_validated(kw):
    with pytest.raises(StreamError):
        StreamConfig(**kw)


def test_random_input_never_breaks_the_books():
    rng = random.Random(11)
    for _ in range(5):
        faults = FaultConfig(
            rng.random() / 2, rng.random() / 2, rng.random() * 120, seed=rng.randint(0, 99)
        )
        report = run_stream(
            rows(400), Script(), StreamConfig(max_lateness=rng.random() * 60, faults=faults)
        )
        assert report["balanced"]


def test_events_are_produced_lazily_with_bounded_lookahead():
    """The generator must not read the whole source before yielding: memory stays bounded."""
    consumed = 0

    def source():
        nonlocal consumed
        for r in rows(500):
            consumed += 1
            yield r

    gen = iter_events(source(), FaultConfig(jitter_seconds=5, duplicate_rate=0.1, seed=3))
    peak_ahead = 0
    for i, _ in enumerate(gen):
        peak_ahead = max(peak_ahead, consumed - i)
    assert peak_ahead < 50
