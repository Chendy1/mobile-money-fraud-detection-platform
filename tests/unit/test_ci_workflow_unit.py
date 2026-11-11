"""A CI job that runs pip must set up its own Python (the runner's system Python refuses pip)."""

from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def test_every_job_that_runs_pip_sets_up_python_first():
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    for name, job in jobs.items():
        steps = job["steps"]
        uses = [
            i
            for i, s in enumerate(steps)
            if str(s.get("uses", "")).startswith("actions/setup-python")
        ]
        pips = [i for i, s in enumerate(steps) if "pip install" in str(s.get("run", ""))]
        if pips:
            assert uses and min(uses) < min(pips), f"job {name} runs pip without setup-python first"
