"""Phase 19 building blocks: load plans, statistics, the sender and the report."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mmrisk.loadtest import plan, report
from mmrisk.loadtest.cli import main as lt_main
from mmrisk.loadtest.runner import GIVE_UP_AFTER, HttpSender, run, run_worker, wire
from mmrisk.loadtest.stats import (
    Sample,
    Slo,
    classify,
    knee,
    percentile,
    summarize_segment,
    verdict,
)

# ---- plans ----


def test_a_constant_profile_is_one_segment_and_a_step_profile_one_per_rate():
    assert plan.segments_for("constant", rate=10, seconds=5) == [plan.Segment(0, 5, 10)]
    steps = plan.segments_for("step", rates=(5, 10, 20), step_seconds=4)
    assert [(s.start, s.end, s.rate) for s in steps] == [(0, 4, 5), (4, 8, 10), (8, 12, 20)]


def test_a_ramp_rises_evenly_from_start_to_end_and_a_spike_returns_to_base():
    ramp = plan.segments_for("ramp", rate=10, peak=50, seconds=5)
    assert [s.rate for s in ramp] == [10, 20, 30, 40, 50] and ramp[-1].end == 5
    spike = plan.segments_for("spike", rate=10, peak=100, step_seconds=3)
    assert [(s.start, s.rate) for s in spike] == [(0, 10), (3, 100), (6, 10)]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"profile": "x"}, "profile must be"),
        ({"profile": "constant", "rate": 0, "seconds": 5}, "above 0"),
        ({"profile": "constant", "rate": float("nan"), "seconds": 5}, "above 0"),
        ({"profile": "constant", "rate": 5000, "seconds": 5}, "limit"),
        ({"profile": "constant", "rate": 5, "seconds": 0.5}, "at least 1 second"),
        ({"profile": "constant", "rate": 5, "seconds": 901}, "at most"),
        ({"profile": "step", "rates": (5,), "step_seconds": 5}, "two rates"),
        ({"profile": "step", "rates": (10, 5), "step_seconds": 5}, "increase"),
        ({"profile": "step", "rates": (5, 5), "step_seconds": 5}, "increase"),
        ({"profile": "ramp", "rate": 20, "peak": 10, "seconds": 5}, "above where it starts"),
        ({"profile": "ramp", "rate": 20, "peak": 20, "seconds": 5}, "above where it starts"),
        ({"profile": "spike", "rate": 20, "peak": 20, "step_seconds": 5}, "above the base"),
    ],
)
def test_bad_profiles_are_refused_with_a_reason(kwargs, message):
    with pytest.raises(plan.PlanError, match=message):
        plan.segments_for(**kwargs)


def test_the_limits_are_inclusive():
    assert plan.segments_for("constant", rate=plan.MAX_RATE, seconds=1)
    assert plan.segments_for("constant", rate=1, seconds=plan.MAX_SECONDS)


def test_even_arrivals_are_evenly_spaced_and_counted_per_segment():
    segs = plan.segments_for("step", rates=(2, 4), step_seconds=2)
    pairs = plan.offsets(segs)
    assert [t for t, i in pairs if i == 0] == [0.0, 0.5, 1.0, 1.5]
    assert [t for t, i in pairs if i == 1] == [2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75]
    assert [t for t, _ in pairs] == sorted(t for t, _ in pairs)


def test_poisson_arrivals_are_reproducible_bounded_and_near_the_rate():
    segs = plan.segments_for("constant", rate=100, seconds=20)
    a, b = plan.offsets(segs, "poisson", 7), plan.offsets(segs, "poisson", 7)
    assert a == b and a != plan.offsets(segs, "poisson", 8)
    assert all(0 <= t < 20 for t, _ in a) and 1800 < len(a) < 2200
    with pytest.raises(plan.PlanError, match="arrivals"):
        plan.offsets(segs, "bursty")


def test_one_customer_always_goes_to_the_same_worker_and_all_workers_are_used():
    assert len({plan.worker_for("C000123", 8) for _ in range(60)}) == 1
    assert plan.worker_for("C000123", 8) == 0  # fixed across runs and machines (crc32)
    used = {plan.worker_for(f"C{i:06d}", 8) for i in range(200)}
    assert used == set(range(8))
    assert plan.worker_for("C1", 1) == 0


def txns(n, customers=5):
    return [
        {"transaction_id": f"T{i:05d}", "customer_id": f"C{i % customers:06d}"} for i in range(n)
    ]


def test_a_plan_keeps_each_customers_transactions_in_order_on_one_worker():
    segs = plan.segments_for("constant", rate=20, seconds=5)
    reqs = plan.build(txns(150), segs, 4)
    assert len(reqs) == 100 and [r.index for r in reqs] == list(range(100))
    by_customer: dict[str, list] = {}
    for r in reqs:
        by_customer.setdefault(r.txn["customer_id"], []).append(r)
    for rs in by_customer.values():
        assert len({r.worker for r in rs}) == 1
        assert [r.offset for r in rs] == sorted(r.offset for r in rs)
        assert [r.index for r in rs] == sorted(r.index for r in rs)


def test_a_plan_needing_more_transactions_than_exist_says_so():
    segs = plan.segments_for("constant", rate=20, seconds=5)
    with pytest.raises(plan.PlanError, match="needs 100 transactions and the stream has 99"):
        plan.build(txns(99), segs, 2)
    assert len(plan.build(txns(100), segs, 2)) == 100
    with pytest.raises(plan.PlanError, match="workers"):
        plan.build(txns(100), segs, 0)


# ---- statistics ----


def test_percentiles_use_the_nearest_rank_rule():
    values = list(range(1, 101))
    assert [percentile(values, q) for q in (50, 95, 99, 100)] == [50, 95, 99, 100]
    assert percentile([5, 1, 3], 50) == 3 and percentile([7], 99) == 7
    assert percentile([], 50) is None
    assert percentile(values, 0.1) == 1  # never below the smallest


@pytest.mark.parametrize(
    "status, name",
    [(200, "ok"), (204, "ok"), (0, "network"), (429, "rate_limited"), (401, "auth"),
     (403, "auth"), (409, "conflict"), (404, "client_error"), (422, "client_error"),
     (500, "server_error"), (503, "server_error")],
)  # fmt: skip
def test_statuses_fall_into_classes(status, name):
    assert classify(status) == name


def s(i, intended, start, end, status=200, seg=0, dup=False):
    return Sample(i, seg, intended, start, end, status, dup)


def test_latency_is_measured_from_the_intended_start_and_lateness_is_reported():
    # meant to start at 1.0, sent at 1.5 (the worker was busy), answered at 1.6
    seg = summarize_segment([s(0, 1.0, 1.5, 1.6)], 10, 1, 1.0, Slo())
    assert seg["latency_ms_from_intended_start"]["p50"] == pytest.approx(600)
    assert seg["latency_ms_service"]["p50"] == pytest.approx(100)
    assert seg["start_lag_ms_p95"] == pytest.approx(500) and seg["saturated"] is True


def test_only_successful_answers_count_toward_latency_and_errors_are_classed():
    samples = [s(0, 0, 0, 0.01), s(1, 0.1, 0.1, 0.11), s(2, 0.2, 0.2, 0.7, status=503),
               s(3, 0.3, 0.3, 0.3, status=0), s(4, 0.4, 0.4, 0.41, status=429)]  # fmt: skip
    seg = summarize_segment(samples, 1, 5, 5.0, Slo())
    assert seg["ok"] == 2 and seg["errors"] == {"server_error": 1, "network": 1, "rate_limited": 1}
    assert seg["error_rate"] == pytest.approx(0.6)
    assert seg["latency_ms_from_intended_start"]["max"] == pytest.approx(10)  # the 503 is not in it


def test_achieved_rate_counts_the_time_a_backlog_takes_to_clear():
    on_time = [s(i, i * 0.1, i * 0.1, i * 0.1 + 0.01) for i in range(10)]
    assert summarize_segment(on_time, 1, 10, 10.0, Slo())["achieved_rate"] == pytest.approx(10)
    behind = [s(i, i * 0.1, i * 0.2, i * 0.2 + 0.01) for i in range(10)]  # finishes at 1.81 s
    seg = summarize_segment(behind, 1, 10, 10.0, Slo())
    assert seg["achieved_rate"] == pytest.approx(10 / 1.81)
    later = [s(i, 5 + i * 0.1, 5 + i * 0.2, 5 + i * 0.2 + 0.01) for i in range(10)]
    seg = summarize_segment(later, 1, 10, 10.0, Slo(), start=5.0)  # measured from its own start
    assert seg["achieved_rate"] == pytest.approx(10 / 1.81)


def test_duplicates_are_counted_and_an_empty_segment_does_not_divide_by_zero():
    seg = summarize_segment([s(0, 0, 0, 0.01, dup=True), s(1, 0.1, 0.1, 0.11)], 1, 2, 2.0, Slo())
    assert seg["duplicates"] == 1
    empty = summarize_segment([], 1, 0, 2.0, Slo())
    assert empty["sent"] == 0 and empty["error_rate"] == 0 and not empty["slo"]["met"]


def seg_with(p95=50.0, p99=100.0, error_rate=0.0, achieved=10.0, offered=10.0, sent=100):
    return {"latency_ms_from_intended_start": {"p95": p95, "p99": p99}, "error_rate": error_rate,
            "achieved_rate": achieved, "offered_rate": offered, "sent": sent}  # fmt: skip


def test_each_target_is_inclusive_and_each_one_alone_can_fail():
    slo = Slo(p95_ms=100, p99_ms=250, max_error_rate=0.01, min_achieved=0.95)
    assert verdict(seg_with(p95=100, p99=250, error_rate=0.01, achieved=9.5), slo)["met"]
    assert not verdict(seg_with(p95=100.1), slo)["p95"]
    assert not verdict(seg_with(p99=250.1), slo)["p99"]
    assert not verdict(seg_with(error_rate=0.0101), slo)["errors"]
    assert not verdict(seg_with(achieved=9.49), slo)["throughput"]
    assert not verdict(seg_with(sent=0), slo)["errors"]
    assert not verdict(seg_with(p95=None), slo)["p95"]


def met(rate, ok=True):
    return {"offered_rate": rate, "slo": {"met": ok}}


def test_the_knee_is_the_last_rate_before_the_first_miss():
    assert knee([met(10), met(20), met(40, False), met(80, False)]) == {
        "offered_rate": 20, "limited": True}  # fmt: skip
    assert knee([met(10), met(20)]) == {"offered_rate": 20, "limited": False}
    assert knee([met(10, False), met(20)]) is None
    assert knee([met(10), met(20, False), met(40)]) == {"offered_rate": 10, "limited": True}
    assert knee([]) is None


# ---- the worker loop, with a fake clock ----


class FakeTime:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def req(i, offset, customer="C1", worker=0):
    return plan.Request(i, offset, 0, worker, {"transaction_id": f"T{i}", "customer_id": customer})


def test_a_worker_waits_for_each_intended_time_and_records_times_from_the_base():
    t = FakeTime()

    def sender(txn):
        t.now += 0.02
        return 200, False

    out = run_worker([req(0, 0.5), req(1, 1.0)], 100.0, sender, t.clock, t.sleep)
    assert t.sleeps == [pytest.approx(0.5), pytest.approx(0.48)]
    assert [(o.intended, o.start, o.end) for o in out] == [
        (0.5, pytest.approx(0.5), pytest.approx(0.52)),
        (1.0, pytest.approx(1.0), pytest.approx(1.02)),
    ]


def test_a_slow_answer_delays_the_next_request_but_not_its_intended_time():
    t = FakeTime()
    calls = []

    def sender(txn):
        calls.append(txn["transaction_id"])
        t.now += 1.0 if txn["transaction_id"] == "T0" else 0.01
        return 200, False

    out = run_worker([req(0, 0.0), req(1, 0.1), req(2, 0.2)], 100.0, sender, t.clock, t.sleep)
    assert calls == ["T0", "T1", "T2"] and t.sleeps == []  # never idle: the second is already late
    assert out[1].intended == 0.1 and out[1].start == pytest.approx(1.0)
    seg = summarize_segment(out, 1, 3, 3.0, Slo())
    assert seg["latency_ms_from_intended_start"]["max"] > 900  # the stall shows in later requests
    assert seg["start_lag_ms_p95"] > 800


def test_a_worker_gives_up_after_repeated_connection_failures_and_tells_the_others():
    t, stop = FakeTime(), threading.Event()
    items = [req(i, i * 0.01) for i in range(GIVE_UP_AFTER + 10)]
    out = run_worker(items, 100.0, lambda txn: (0, False), t.clock, t.sleep, stop)
    assert len(out) == GIVE_UP_AFTER and stop.is_set()
    other = run_worker([req(0, 0.0)], 100.0, lambda txn: (200, False), t.clock, t.sleep, stop)
    assert other == []  # already stopped


def test_one_success_resets_the_failure_count():
    t, answers = FakeTime(), iter([0] * (GIVE_UP_AFTER - 1) + [200] + [0] * (GIVE_UP_AFTER - 1))
    items = [req(i, 0) for i in range(2 * GIVE_UP_AFTER - 1)]
    out = run_worker(items, 100.0, lambda txn: (next(answers), False), t.clock, t.sleep)
    assert len(out) == len(items)


def test_the_threaded_run_sends_everything_once_and_in_order_per_customer():
    reqs = plan.build(txns(60, customers=6), plan.segments_for("constant", rate=60, seconds=1), 3)
    seen, lock = [], threading.Lock()

    def sender(txn):
        with lock:
            seen.append(txn["transaction_id"])
        return 200, False

    samples, not_sent = run(reqs, sender, 3)
    assert not_sent == 0 and sorted(seen) == [f"T{i:05d}" for i in range(60)]
    for c in range(6):
        ids = [x for x in seen if int(x[1:]) % 6 == c]
        assert ids == sorted(ids)
    assert [x.index for x in samples] == list(range(60))


def test_the_threaded_run_reports_requests_that_were_never_sent():
    reqs = plan.build(txns(200, customers=1), plan.segments_for("constant", rate=200, seconds=1), 1)
    samples, not_sent = run(reqs, lambda txn: (0, False), 1)
    assert len(samples) == GIVE_UP_AFTER and not_sent == 200 - GIVE_UP_AFTER


# ---- the real sender, against a small local server ----


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers["content-length"])
        body = json.loads(self.rfile.read(n))
        Handler.seen.append(
            (self.path, self.headers.get("authorization"), body, self.client_address)
        )
        code = {"T-bad": 422, "T-busy": 503, "T-limit": 429}.get(body["transaction_id"], 200)
        out = json.dumps({"duplicate": body["transaction_id"] == "T-dup"}).encode()
        self.send_response(code)
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def server():
    Handler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def test_the_sender_posts_the_wire_form_with_the_key_and_reads_status_and_duplicate(server):
    from datetime import datetime

    sender = HttpSender(server, key="abc")
    txn = {"transaction_id": "T-dup", "timestamp": datetime(2025, 1, 2, 3, 4, 5), "amount_kes": 5}
    assert sender(txn) == (200, True)
    assert sender({"transaction_id": "T-ok"}) == (200, False)
    assert [sender({"transaction_id": t})[0] for t in ("T-bad", "T-busy", "T-limit")] == [
        422,
        503,
        429,
    ]
    path, auth, body, _ = Handler.seen[0]
    assert (
        path == "/v1/score" and auth == "Bearer abc" and body["timestamp"] == "2025-01-02T03:04:05"
    )
    assert wire(txn)["timestamp"] == "2025-01-02T03:04:05"
    sender.close()


def test_the_sender_keeps_one_connection_per_thread_and_sends_no_key_when_none_is_given(server):
    sender = HttpSender(server)
    for i in range(5):
        sender({"transaction_id": f"T{i}"})
    assert len({addr for *_, addr in Handler.seen}) == 1 and Handler.seen[0][1] is None
    ports = []

    def other():
        sender({"transaction_id": "TX"})
        ports.append(Handler.seen[-1][3])
        sender.close()

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert ports[0] != Handler.seen[0][3]
    sender.close()


def test_the_sender_reports_status_zero_when_nothing_answers_or_the_answer_is_too_slow():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert HttpSender(f"http://127.0.0.1:{port}", timeout=1)({"transaction_id": "T"}) == (0, False)
    with socket.socket() as sock:  # accepts but never answers
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        slow = HttpSender(f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=0.3)
        assert slow({"transaction_id": "T"}) == (0, False)


def test_a_bad_url_is_refused():
    for url in ("ftp://x", "127.0.0.1:8000", "http://"):
        with pytest.raises(ValueError, match="http"):
            HttpSender(url)


# ---- the report ----


def build_report(samples, planned=(2,), not_sent=0, rate=2.0):
    segs = [plan.Segment(0, 1, rate)]
    return report.build(
        name="t", mode="bench", settings={"auth": False}, segments=segs, samples=samples,
        planned=list(planned), not_sent=not_sent, slo=Slo(), server={"agree": True},
    )  # fmt: skip


def test_a_clean_run_has_no_warnings_and_a_table_with_one_row_per_segment():
    rep = build_report([s(0, 0, 0, 0.01), s(1, 0.5, 0.5, 0.51)])
    assert rep["warnings"] == [] and rep["totals"] == {
        "planned": 2,
        "sent": 2,
        "ok": 2,
        "not_sent": 0,
    }
    md = report.markdown(rep)
    assert "| 2.0 | 2.0 | 2 | 0 |" in md and "ASSUMED" in md and rep["caveat"] in md
    assert "limit was not reached" in md and "Server's own count" in md


def test_each_problem_gets_its_own_warning():
    base = [s(0, 0, 0, 0.01), s(1, 0.5, 0.5, 0.51)]
    assert "never sent" in build_report(base, not_sent=3)["warnings"][0]
    dup = build_report([s(0, 0, 0, 0.01, dup=True), base[1]])["warnings"]
    assert any("duplicate" in w for w in dup)
    conflict = build_report([s(0, 0, 0, 0.01, status=409), base[1]])["warnings"]
    assert any("409" in w for w in conflict)
    late = build_report([s(0, 0, 1.0, 1.01), base[1]])["warnings"]
    assert any("started late" in w for w in late)


def test_the_reading_says_when_the_targets_were_missed_and_where():
    bad = [s(0, 0, 0, 0.5), s(1, 0.5, 0.5, 1.0)]
    assert "already missed" in report.markdown(build_report(bad))


def test_reports_are_written_as_json_and_markdown(tmp_path):
    rep = build_report([s(0, 0, 0, 0.01), s(1, 0.5, 0.5, 0.51)])
    j, m = report.write(rep, tmp_path / "out")
    assert json.loads(j.read_text())["name"] == "t" and m.read_text().startswith("# Load test: t")


# ---- the command ----


def test_a_non_local_url_needs_allow_external_and_a_start_time(capsys):
    assert lt_main(["run", "--url", "http://example.com:8000", "--from", "2025-12-01"]) == 2
    assert "--allow-external" in capsys.readouterr().err
    assert lt_main(["run", "--url", "http://127.0.0.1:1"]) == 2
    assert "--from is required" in capsys.readouterr().err
    assert lt_main(["run", "--url", "nonsense", "--from", "2025-12-01"]) == 2


def test_bad_numbers_are_refused_before_anything_is_sent(capsys, tmp_path):
    assert lt_main(["run", "--from", "2025-12-01", "--lake", str(tmp_path / "none")]) == 2
    assert "lake" in capsys.readouterr().err
    assert lt_main(["show", str(tmp_path / "none.json")]) == 2


def test_show_prints_a_saved_report(tmp_path, capsys):
    j, _ = report.write(build_report([s(0, 0, 0, 0.01), s(1, 0.5, 0.5, 0.51)]), tmp_path)
    assert lt_main(["show", str(j)]) == 0 and "# Load test: t" in capsys.readouterr().out


def test_running_the_module_works():
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-m", "mmrisk.loadtest", "--help"],
                          capture_output=True, text=True, timeout=60)  # fmt: skip
    assert done.returncode == 0 and "bench" in done.stdout


# ---- stopping the server we started ----


def test_stop_process_ends_a_running_process_and_is_safe_on_one_that_already_ended():
    import subprocess
    import sys
    import time

    from mmrisk.loadtest.cli import stop_process

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    t0 = time.monotonic()
    stop_process(proc)
    assert proc.poll() is not None and time.monotonic() - t0 < 10
    stop_process(proc)  # already gone: nothing happens, nothing raises


def test_on_windows_the_whole_process_tree_is_ended(monkeypatch):
    """Cannot run Windows here, so the command is checked: /T is what ends the child."""
    from mmrisk.loadtest import cli

    calls = []

    class Proc:
        pid = 4321

        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    cli.stop_process(Proc())
    assert calls == [["taskkill", "/PID", "4321", "/T", "/F"]]  # not a bare terminate()
    monkeypatch.setattr(cli.os, "name", "posix")
    calls.clear()
    cli.stop_process(Proc())
    assert calls == ["terminate"]


def test_the_bench_folder_is_made_so_that_a_failed_cleanup_cannot_change_the_result(
    monkeypatch, tmp_path
):
    """The Windows failure: the report was written, then removing the folder raised and the run
    was reported as an error. The folder must be made with ignore_cleanup_errors."""
    import tempfile
    from datetime import datetime

    from mmrisk.loadtest import cli

    seen = {}
    real = tempfile.TemporaryDirectory

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    def no_server(*args, **kwargs):
        raise OSError("no server in this test")

    monkeypatch.setattr(cli.tempfile, "TemporaryDirectory", spy)
    monkeypatch.setattr(cli, "_stream", lambda a: (datetime(2025, 1, 1), []))
    monkeypatch.setattr(cli.subprocess, "Popen", no_server)
    assert lt_main(["bench", "--lake", str(tmp_path)]) == 2
    assert seen.get("ignore_cleanup_errors") is True
