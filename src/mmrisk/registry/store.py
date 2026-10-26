"""Model registry on the local disk. No server, no database.

Layout under the registry root:

    versions/v0001/risk_model.joblib, risk_model.json, version.json
    production.json      the version that serves traffic (one small file, replaced atomically)
    history.jsonl        every register / promote / rollback / stage change, appended, never edited

`production.json` is the only thing a reader needs: point `--model-dir` at the registry root and
`resolve_model_dir` follows it to the version directory.

What this is not: it has no locking (one writer at a time) and its checksum only detects a file
that no longer matches what was registered. Whoever can write the registry can rewrite the
checksum too, and a joblib file is a pickle: only use a registry that you created.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from mmrisk.models.bundle import BUNDLE_FILE, META_FILE

PRODUCTION_FILE = "production.json"
HISTORY_FILE = "history.jsonl"
VERSION_FILE = "version.json"
STAGES = ("candidate", "approved", "rejected")
COPIED = (BUNDLE_FILE, META_FILE, "advanced_metrics.json")  # the last one is optional


class RegistryError(ValueError):
    """A registry operation that cannot be done (and why)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Registry:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- reading -------------------------------------------------------------------------
    @property
    def versions_dir(self) -> Path:
        return self.root / "versions"

    def ids(self) -> list[str]:
        if not self.versions_dir.exists():
            return []
        return sorted(
            p.name for p in self.versions_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
        )

    def get(self, version: str) -> dict:
        path = self.versions_dir / version / VERSION_FILE
        if not path.exists():
            raise RegistryError(f"no version {version} in {self.root}")
        return json.loads(path.read_text(encoding="utf-8"))

    def versions(self) -> list[dict]:
        return [self.get(v) for v in self.ids()]

    def version_dir(self, version: str) -> Path:
        self.get(version)
        return self.versions_dir / version

    def production(self) -> dict | None:
        path = self.root / PRODUCTION_FILE
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise RegistryError(f"{path} is not valid JSON") from None

    def history(self) -> list[dict]:
        path = self.root / HISTORY_FILE
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def verify(self, version: str) -> None:
        """Raise when the stored model file no longer matches the checksum taken at register."""
        info = self.get(version)
        target = self.versions_dir / version / BUNDLE_FILE
        if not target.exists():
            raise RegistryError(f"{version} has no {BUNDLE_FILE}")
        if _sha256(target) != info["sha256"]:
            raise RegistryError(f"{version}: {BUNDLE_FILE} does not match its recorded checksum")

    def production_dir(self) -> Path:
        prod = self.production()
        if prod is None:
            raise RegistryError(
                f"no production model in {self.root}; register one with "
                "python -m mmrisk.registry bootstrap"
            )
        self.verify(prod["version"])
        return self.versions_dir / prod["version"]

    # ---- writing -------------------------------------------------------------------------
    def _log(self, **event) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / HISTORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": _now(), **event}, default=str) + "\n")

    def _next_id(self) -> str:
        nums = [int(v[1:]) for v in self.ids() if v[1:].isdigit()]
        return f"v{(max(nums) if nums else 0) + 1:04d}"

    def register(
        self,
        model_dir: Path,
        *,
        source: str,
        stage: str = "candidate",
        parent: str | None = None,
        metrics: dict | None = None,
        gates: list | None = None,
        quick: bool = False,
        note: str = "",
    ) -> str:
        """Copy a saved model bundle in as a new version and return its id."""
        model_dir = Path(model_dir)
        if stage not in STAGES:
            raise RegistryError(f"stage must be one of {STAGES}")
        for name in (BUNDLE_FILE, META_FILE):
            if not (model_dir / name).exists():
                raise RegistryError(f"{model_dir} has no {name}; not a saved model")
        if parent is not None:
            self.get(parent)
        meta = json.loads((model_dir / META_FILE).read_text(encoding="utf-8"))
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        staging = self.versions_dir / f".tmp-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        try:
            for name in COPIED:
                if (model_dir / name).exists():
                    shutil.copy2(model_dir / name, staging / name)
            info = {
                "created_at": _now(),
                "source": source,
                "stage": stage,
                "parent": parent,
                "feature_version": meta.get("feature_version"),
                "sha256": _sha256(staging / BUNDLE_FILE),
                "quick": bool(quick),
                "note": note,
                "metrics": metrics or {},
                "gates": gates or [],
            }
            while True:  # claim the next id without overwriting a version made meanwhile
                version = self._next_id()
                info["version"] = version
                _write_json(staging / VERSION_FILE, info)
                try:
                    os.rename(staging, self.versions_dir / version)
                    break
                except OSError:
                    if not (self.versions_dir / version).exists():
                        raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._log(action="register", version=version, source=source, stage=stage, parent=parent)
        return version

    def set_stage(self, version: str, stage: str, note: str = "") -> None:
        if stage not in STAGES:
            raise RegistryError(f"stage must be one of {STAGES}")
        info = self.get(version)
        before, info["stage"] = info["stage"], stage
        _write_json(self.versions_dir / version / VERSION_FILE, info)
        self._log(action="stage", version=version, before=before, after=stage, note=note)

    def promote(self, version: str, reason: str, force: bool = False) -> dict:
        """Make `version` the production model.

        Refuses a version that has not passed the gates ("approved") or was trained in the quick
        smoke mode, unless `force` is set. The reason is required and goes in the history.
        """
        if not reason.strip():
            raise RegistryError("a reason is required to promote a model")
        info = self.get(version)
        self.verify(version)
        if not force:
            if info["stage"] != "approved":
                raise RegistryError(
                    f"{version} is '{info['stage']}', not 'approved'; use --force to override"
                )
            if info["quick"]:
                raise RegistryError(
                    f"{version} was trained in quick mode (a smoke test); use --force to override"
                )
        current = self.production()
        if current is not None and current["version"] == version:
            raise RegistryError(f"{version} is already the production model")
        pointer = {
            "version": version,
            "promoted_at": _now(),
            "previous": current["version"] if current else None,
            "reason": reason,
            "forced": bool(force),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        _write_json(self.root / PRODUCTION_FILE, pointer)
        self._log(action="promote", version=version, previous=pointer["previous"], reason=reason,
                  forced=bool(force))  # fmt: skip
        return pointer

    def rollback(self, reason: str) -> dict:
        """Switch production back to the version it replaced (running it twice switches again)."""
        if not reason.strip():
            raise RegistryError("a reason is required to roll back")
        current = self.production()
        if current is None or not current.get("previous"):
            raise RegistryError("there is no earlier production version to return to")
        target = current["previous"]
        self.verify(target)
        pointer = {
            "version": target,
            "promoted_at": _now(),
            "previous": current["version"],
            "reason": reason,
            "forced": False,
        }
        _write_json(self.root / PRODUCTION_FILE, pointer)
        self._log(action="rollback", version=target, previous=current["version"], reason=reason)
        return pointer


def resolve_model_dir(path: Path) -> Path:
    """The directory holding the model files: `path` itself, or the production version of a
    registry rooted at `path`."""
    path = Path(path)
    if (path / PRODUCTION_FILE).exists() or (path / "versions").is_dir():
        return Registry(path).production_dir()  # raises when nothing is in production
    return path
