"""Backup and restore of what a deployment cannot rebuild: the model registry and its history,
drift and retraining records, the monitoring baseline and the audit log.

Not included by default: the data lake and the predictions (rebuildable or large;
`--with-predictions` adds the latter) and the secrets (`--include-secrets`; the archive is then
as sensitive as the secrets and is not encrypted). Restore verifies every file against the
manifest before it writes anything, refuses unsafe paths and links, and does not overwrite
without `--force`.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from mmrisk.deploy import DeployError

DEFAULT_ROOTS = ("models/registry", "data/retrain", "data/drift", "data/monitoring", "data/audit")
OPTIONAL = {"predictions": "data/predictions", "secrets": "secrets"}
MANIFEST = "MANIFEST.json"
MAX_FILE_BYTES = 2 * 1024**3
MAX_TOTAL_BYTES = 8 * 1024**3
FORMAT = 1


def _sha256(fh, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    while block := fh.read(chunk):
        h.update(block)
    return h.hexdigest()


def _files_under(root: Path, rel: str) -> list[Path]:
    base = root / rel
    if base.is_symlink():
        raise DeployError(f"{rel} is a link; refusing to back it up")
    if not base.exists():
        return []
    found = []
    for p in sorted(base.rglob("*")):
        if p.is_symlink():
            raise DeployError(f"{p.relative_to(root).as_posix()} is a link; refusing to back it up")
        if p.is_file():
            found.append(p)
    return found


def backup(
    root: Path, out: Path, with_predictions: bool = False, include_secrets: bool = False
) -> dict:
    root, out = Path(root), Path(out)
    if out.exists():
        raise DeployError(f"{out} already exists; backups are not overwritten")
    roots = list(DEFAULT_ROOTS)
    if with_predictions:
        roots.append(OPTIONAL["predictions"])
    if include_secrets:
        roots.append(OPTIONAL["secrets"])
    entries, skipped = [], []
    for rel in roots:
        files = _files_under(root, rel)
        if not files:
            skipped.append(rel)
        for p in files:
            size = p.stat().st_size
            if size > MAX_FILE_BYTES:
                raise DeployError(f"{p.name} is {size} bytes; the limit is {MAX_FILE_BYTES}")
            with open(p, "rb") as fh:
                entries.append({"path": p.relative_to(root).as_posix(), "sha256": _sha256(fh),
                                "size": size})  # fmt: skip
    if not entries:
        raise DeployError("nothing to back up: none of " + ", ".join(roots) + " has files")
    if sum(e["size"] for e in entries) > MAX_TOTAL_BYTES:
        raise DeployError("the backup would be larger than the limit")
    manifest = {"format": FORMAT, "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                "includes_secrets": include_secrets, "files": entries}  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    with tarfile.open(tmp, "w:gz") as tar:
        raw = json.dumps(manifest, indent=1).encode()
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(raw)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(raw))
        for e in entries:
            tar.add(root / e["path"], arcname=e["path"], recursive=False,
                    filter=_clean)  # fmt: skip
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, out)
    return {"archive": str(out), "files": len(entries), "bytes": sum(e["size"] for e in entries),
            "skipped_empty": skipped, "includes_secrets": include_secrets}  # fmt: skip


def _clean(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o600 if info.isfile() else 0o700
    return info


def _safe_rel(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if (not name or p.is_absolute() or "\\" in name or ":" in name
            or any(part in ("", ".", "..") for part in name.split("/"))):  # fmt: skip
        raise DeployError(f"unsafe path in the archive: {name!r}")
    return p


def restore(
    archive: Path, target: Path, force: bool = False, include_secrets: bool = False,
    dry_run: bool = False,
) -> dict:  # fmt: skip
    archive, target = Path(archive), Path(target).resolve()
    try:
        tar = tarfile.open(archive, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise DeployError(f"cannot read {archive}: {exc}") from None
    with tar:
        members = {m.name: m for m in tar.getmembers()}
        if MANIFEST not in members or not members[MANIFEST].isfile():
            raise DeployError("not a backup made by this tool: no manifest")
        if members[MANIFEST].size > 50 * 1024 * 1024:
            raise DeployError("the manifest is too large")
        try:
            manifest = json.loads(tar.extractfile(MANIFEST).read())
            files = {e["path"]: e for e in manifest["files"]}
        except (ValueError, KeyError, TypeError):
            raise DeployError("the manifest is damaged") from None
        if manifest.get("format") != FORMAT:
            raise DeployError(f"backup format {manifest.get('format')!r} is not supported")
        data_members = {n: m for n, m in members.items() if n != MANIFEST}
        for name, m in data_members.items():
            _safe_rel(name)
            if not m.isfile():  # no links, devices or directories as entries
                raise DeployError(f"{name} is not a plain file")
        if set(data_members) != set(files):
            extra = sorted(set(data_members) ^ set(files))[:3]
            raise DeployError(f"the archive and its manifest disagree about: {', '.join(extra)}")
        allowed = (*DEFAULT_ROOTS, *OPTIONAL.values())
        for name in files:
            if not any(name.startswith(r + "/") for r in allowed):
                raise DeployError(f"{name} is outside the folders a backup may restore")
        # verify everything before writing anything
        for name, e in files.items():
            m = data_members[name]
            if m.size != e["size"] or m.size > MAX_FILE_BYTES:
                raise DeployError(f"{name}: size differs from the manifest")
            if _sha256(tar.extractfile(m)) != e["sha256"]:
                raise DeployError(f"{name}: contents differ from the manifest (damaged or altered)")
        chosen = [n for n in files if include_secrets or not n.startswith("secrets/")]
        skipped = [n for n in files if n not in chosen]
        clashes = [n for n in chosen if (target / n).exists()]
        if clashes and not force:
            raise DeployError(
                f"{len(clashes)} files already exist (for example {clashes[0]}); "
                "--force replaces them"
            )
        for name in chosen:  # no path may leave the target through an existing link
            dest = (target / name).resolve()
            if target != dest and target not in dest.parents:
                raise DeployError(f"{name} would be written outside {target}")
        if dry_run:
            return {"verified": len(files), "would_restore": len(chosen), "skipped": skipped}
        for name in chosen:
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".restoring")
            with open(tmp, "wb") as fh:
                fh.write(tar.extractfile(data_members[name]).read())
            os.replace(tmp, dest)
    return {"verified": len(files), "restored": len(chosen), "skipped": skipped}
