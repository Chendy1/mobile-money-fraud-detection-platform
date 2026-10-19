"""Experiment tracking behind a small interface, so the pipeline runs with or without MLflow.

`NullTracker` keeps everything in memory (it is what the tests and `--no-tracking` use);
`MlflowTracker` writes to a local MLflow store. Tracking is only ever *observing* the run: no
result may depend on which tracker is used (a test checks that).

Only local stores are accepted. Remote tracking servers need credentials, and this project keeps
credentials out of the repository and out of the code.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

DEFAULT_URI = "sqlite:///mlflow.db"
_BATCH = 90  # MLflow limits the size of one logging request


class TrackingError(ValueError):
    pass


def check_tracking_uri(uri: str) -> str:
    """Accept a local sqlite file, a local folder or a file: URI; refuse anything remote."""
    if not uri or not uri.strip():
        raise TrackingError("tracking uri is empty")
    uri = uri.strip()
    if re.match(r"^[A-Za-z]:[\\/]", uri):  # a Windows path such as C:\runs
        return uri
    if uri.lower() in ("databricks", "databricks-uc", "uc"):  # bare names MLflow sends to a remote
        raise TrackingError("only local tracking is supported, 'databricks' is a remote service")
    scheme = uri.split(":", 1)[0].lower() if ":" in uri else ""
    if scheme in ("", "file", "sqlite"):
        return uri
    raise TrackingError(
        "only local tracking is supported (sqlite:///file.db, a folder or file:...), "
        f"got '{scheme}:'"
    )


def _clean(metrics: dict[str, float]) -> tuple[dict[str, float], int]:
    out, skipped = {}, 0
    for k, v in metrics.items():
        if v is None or isinstance(v, bool) or not math.isfinite(float(v)):
            skipped += 1
        else:
            out[k] = float(v)
    return out, skipped


class Tracker:
    """Interface and no-op base."""

    name = "none"
    run_id: str | None = None

    def start(self, run_name: str, tags: dict[str, str]) -> None: ...

    def log_params(self, params: dict) -> None: ...

    def log_metrics(self, metrics: dict[str, float]) -> None: ...

    def set_tags(self, tags: dict[str, str]) -> None: ...

    def log_artifact(self, path: Path, subdir: str | None = None) -> None: ...

    def log_artifacts(self, directory: Path, subdir: str | None = None) -> None: ...

    def end(self, status: str = "FINISHED") -> None: ...


class NullTracker(Tracker):
    """Records what would have been logged; used for tests and for runs without tracking."""

    name = "memory"

    def __init__(self):
        self.run_name: str | None = None
        self.params: dict = {}
        self.metrics: dict[str, float] = {}
        self.tags: dict[str, str] = {}
        self.artifacts: list[tuple[str, str | None]] = []
        self.status: str | None = None
        self.skipped_metrics = 0

    def start(self, run_name, tags):
        self.run_name = run_name
        self.tags.update(tags)

    def log_params(self, params):
        clash = [k for k, v in params.items() if k in self.params and str(self.params[k]) != str(v)]
        if clash:
            raise TrackingError(f"parameter logged twice with different values: {clash}")
        self.params.update(params)

    def log_metrics(self, metrics):
        clean, skipped = _clean(metrics)
        self.skipped_metrics += skipped
        self.metrics.update(clean)

    def set_tags(self, tags):
        self.tags.update(tags)

    def log_artifact(self, path, subdir=None):
        self.artifacts.append((str(path), subdir))

    def log_artifacts(self, directory, subdir=None):
        self.artifacts.append((str(directory), subdir))

    def end(self, status="FINISHED"):
        self.status = status


class MlflowTracker(Tracker):
    name = "mlflow"

    def __init__(self, uri: str = DEFAULT_URI, experiment: str = "mmrisk-risk-model"):
        self.uri = check_tracking_uri(uri)
        self.experiment = experiment
        try:
            import mlflow
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise TrackingError(
                'MLflow is not installed: pip install -e ".[tracking]" or use --no-tracking'
            ) from exc
        self._mlflow = mlflow
        self.skipped_metrics = 0

    def start(self, run_name, tags):
        m = self._mlflow
        m.set_tracking_uri(self.uri)
        m.set_experiment(self.experiment)
        run = m.start_run(run_name=run_name)
        self.run_id = run.info.run_id
        # MLflow records the operating-system user name by default; keep that out of the store
        # (and the path of the script that started the run, for the same reason)
        m.set_tags({"mlflow.user": "local", "mlflow.source.name": "mmrisk.pipeline", **tags})

    def log_params(self, params):
        items = [(k, str(v)) for k, v in params.items()]
        for i in range(0, len(items), _BATCH):
            self._mlflow.log_params(dict(items[i : i + _BATCH]))

    def log_metrics(self, metrics):
        clean, skipped = _clean(metrics)
        self.skipped_metrics += skipped
        items = list(clean.items())
        for i in range(0, len(items), 900):
            self._mlflow.log_metrics(dict(items[i : i + 900]))

    def set_tags(self, tags):
        self._mlflow.set_tags(tags)

    def log_artifact(self, path, subdir=None):
        self._mlflow.log_artifact(str(path), subdir)

    def log_artifacts(self, directory, subdir=None):
        self._mlflow.log_artifacts(str(directory), subdir)

    def end(self, status="FINISHED"):
        self._mlflow.end_run(status=status)
