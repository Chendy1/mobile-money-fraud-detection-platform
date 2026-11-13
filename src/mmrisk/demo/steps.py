"""The demo script as data. Edit here; `docs/demo.md` is checked against these ids."""

from __future__ import annotations

from mmrisk.demo import Paths, Step

TRACKS = ("short", "standard")


def build_steps(p: Paths) -> list[Step]:
    lake, model, reg, out = str(p.lake), str(p.model_dir), str(p.registry), p.out
    store, drift_ref = str(out / "predictions"), str(out / "drift" / "reference.json")
    return [
        Step(
            "prereq",
            "Is everything the demo needs in place?",
            1,
            ("Before an audience, check the ground first: lake, model, registry, model card.",),
            action="prereq",
            tracks=("short", "standard"),
        ),
        Step(
            "quality",
            "Data quality: what happens when bad data enters",
            5,
            (
                "Bad rows are quarantined with a reason during ingestion; this command checks the whole lake.",
                "63 checks, PASS/WARN/FAIL, thresholds written down. Freshness is skipped because this is a replay of history.",
            ),
            commands=(("mmrisk.validation", "check", "--lake", lake, "--no-write"),),
            expect=(r"OVERALL: PASS",),
        ),
        Step(
            "card",
            "The result, and how not to over-read it",
            1,
            (
                "Synthetic data. The test window holds only about a hundred fraud-labelled transactions (the card says exactly), so the intervals are wide.",
                "Point out that LightGBM did NOT beat the random forest on PR-AUC; it is shipped for exact explanations and missing-history handling.",
                "The cost column uses ASSUMED costs: it shows the method, not money.",
            ),
            action="card",
            expect=(r"LightGBM",),
            tracks=("short", "standard"),
        ),
        Step(
            "apicheck",
            "Online scoring equals batch scoring",
            5,
            (
                "The API runs the same feature code as training. This replays history, then scores the last 300 transactions one by one.",
                "The claim to look for: largest probability difference from batch scoring 0, same actions.",
                "Latency is one process on this machine; it is not a capacity claim.",
            ),
            commands=(("mmrisk.api", "check", "--model-dir", model, "--lake", lake),),
            expect=(r"check: PASS", r"same action as batch scoring: True"),
            tracks=("short", "standard"),
        ),
        Step(
            "secure",
            "Security boundaries, checked from outside",
            8,
            (
                "A real API process with TLS and hashed API keys, in a temporary folder, then the same `deploy verify` checks used against the container.",
                "The first five lines are the smoke test; the rest are boundaries: no key refused, wrong key refused, a score key cannot read metrics, the metrics key cannot score, plain HTTP gets no answer.",
                "This is not a security audit; it shows the boundaries are tested, not that none exist.",
            ),
            action="secure",
            tracks=("short", "standard"),
        ),
        Step(
            "simulate",
            "A live-like feed with delivery faults",
            10,
            (
                "Stored transactions are replayed through a bounded queue with seeded faults: jitter, repeats and corrupted copies.",
                "Every event must end as scored, rejected or dropped, and the run says whether the books balance.",
            ),
            commands=(
                (
                    "mmrisk.streaming",
                    "run",
                    "--lake",
                    lake,
                    "--model-dir",
                    model,
                    "--store",
                    store,
                    "--limit",
                    "2000",
                    "--jitter-seconds",
                    "20",
                    "--duplicate-rate",
                    "0.01",
                    "--bad-rate",
                    "0.005",
                    "--report",
                    str(out / "stream_run.json"),
                ),
            ),
            expect=(r"balanced: every event is accounted for",),
        ),
        Step(
            "monitor",
            "Did the stored decisions stay consistent?",
            2,
            (
                "Checks on the prediction store: integrity, gaps, action and risk level agree, flagged rows carry reasons.",
                "These checks see decisions, not correctness: there are no labels yet, and that is stated in the docs.",
            ),
            commands=(
                ("mmrisk.monitoring", "report", "--store", store, "--out", str(out / "monitoring")),
            ),
            expect=(r"PASS\s+predictions exist",),
            needs=("simulate",),
        ),
        Step(
            "drift",
            "Drift: has the input moved? (normal week)",
            4,
            (
                "A reference summary of the validation window, compared with the latest week by the population stability index.",
                "Features that grow with time by construction are listed but not counted, on purpose.",
            ),
            commands=(
                (
                    "mmrisk.drift",
                    "reference",
                    "--lake",
                    lake,
                    "--model-dir",
                    model,
                    "--out",
                    drift_ref,
                ),
                (
                    "mmrisk.drift",
                    "report",
                    "--lake",
                    lake,
                    "--model-dir",
                    model,
                    "--reference",
                    drift_ref,
                    "--out",
                    str(out / "drift"),
                ),
            ),
            ok_codes=(0, 1),
            expect=(r"(no )?drift detected",),
        ),
        Step(
            "drift_shift",
            "Drift: does it notice a shift? (amounts tripled, a demonstration)",
            4,
            (
                "The same detector on the same week with every amount multiplied by 3. This is a demonstration, labelled as one in the report.",
                "Exit code 1 here is the point: the detector fired. A rise of 50% was measured to go unnoticed (ADR-020), so say that too.",
            ),
            commands=(
                (
                    "mmrisk.drift",
                    "reference",
                    "--lake",
                    lake,
                    "--model-dir",
                    model,
                    "--out",
                    drift_ref,
                ),
                (
                    "mmrisk.drift",
                    "report",
                    "--lake",
                    lake,
                    "--model-dir",
                    model,
                    "--reference",
                    drift_ref,
                    "--out",
                    str(out / "drift_shifted"),
                    "--scale-amounts",
                    "3",
                ),
            ),
            ok_codes=(1,),
            expect=(r"MAJOR\s+amount_kes",),
            tracks=("short", "standard"),
        ),
        Step(
            "retrain",
            "Should the model be retrained? (a question with rules, not a reflex)",
            3,
            (
                "Triggers: drift, model age over 90 days, 200 new fraud labels, or forced. Unknown inputs never fire.",
                "A candidate is only ever registered; promotion needs a person and a reason.",
            ),
            commands=(
                (
                    "mmrisk.retrain",
                    "check",
                    "--lake",
                    lake,
                    "--registry",
                    reg,
                    "--out",
                    str(out / "retrain"),
                    "--drift-report",
                    str(out / "drift" / "report.json"),
                ),
            ),
            ok_codes=(0, 1),
            expect=(r"model_age",),
            needs=("drift",),
        ),
        Step(
            "dashcheck",
            "What the dashboard would show (no browser needed)",
            2,
            (
                "The dashboard is read-only over these files. This lists what it found.",
                "To show it: `python -m mmrisk.dashboard run`, then http://127.0.0.1:8501. How it looks in a browser has not been checked by the assistant.",
            ),
            commands=(
                (
                    "mmrisk.dashboard",
                    "check",
                    "--store",
                    store,
                    "--drift-report",
                    str(out / "drift" / "report.json"),
                    "--registry",
                    reg,
                    "--retrain-dir",
                    str(out / "retrain"),
                ),
            ),
            expect=(r"health: PASS",),
            needs=("simulate",),
        ),
        Step(
            "cost",
            "What the cloud design would cost resting (lower bound)",
            1,
            (
                "Nothing was created in AWS. This reads sourced prices and prints a lower bound with what is not priced listed beneath it.",
                "Resting is two items; the interface endpoints that bill by the hour are off by default for exactly this reason.",
            ),
            commands=(
                (
                    "mmrisk.cloudcost",
                    "estimate",
                    "--scenario",
                    "resting",
                    "--prices",
                    str(p.prices),
                ),
            ),
            expect=(r"TOTAL \(lower bound\)",),
            tracks=("short", "standard"),
        ),
    ]


def select(steps: list[Step], track: str, only: tuple[str, ...] = ()) -> list[Step]:
    from mmrisk.demo import DemoError

    if only:
        known = {s.id for s in steps}
        bad = [i for i in only if i not in known]
        if bad:
            raise DemoError(f"unknown step: {', '.join(bad)} (see `python -m mmrisk.demo list`)")
        return [s for s in steps if s.id in only]
    if track not in TRACKS:
        raise DemoError(f"unknown track {track!r}; choose one of {', '.join(TRACKS)}")
    return [s for s in steps if track in s.tracks or track == "standard"]
