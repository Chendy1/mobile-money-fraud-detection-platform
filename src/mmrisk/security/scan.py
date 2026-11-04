"""A small secret scanner for the repository: patterns for well-known credential formats.

It looks for private key blocks, cloud and Git hosting tokens, assignments of long literal values
to names like password or api_key, and files that should never be committed (.env, *.pem, key and
state files). It uses `git ls-files` when it can, so ignored files (data, models, .venv) are not
scanned, and otherwise walks the folder skipping the usual generated ones.

It is not a replacement for a dedicated scanner: it has no entropy checks and no history scan (a
secret committed and later deleted is still in Git's history). A line containing `nosecret` is
skipped. A finding shows at most the first 4 characters of the match.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
             "mlruns", "build", "data", "models", "dist", ".mypy_cache"}  # fmt: skip
MAX_BYTES = 1_000_000
BAD_NAMES = re.compile(
    r"(^|/)(\.env(\..*)?|id_rsa|id_ed25519|.*\.pem|.*\.p12|.*\.pfx|.*\.key|terraform\.tfstate(\..*)?"
    r"|.*\.auto\.tfvars|api_keys\.json|dashboard\.hash)$"
)
ALLOWED_NAMES = re.compile(r"(\.env\.example|\.env\.sample|.*\.example\.key)$")
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "aws access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "aws secret key": re.compile(
        r"aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}\b", re.IGNORECASE
    ),
    "github token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    "slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "project api key": re.compile(r"\bmmk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}\b"),
    "literal secret": re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']"
    ),
}
_PLACEHOLDER = re.compile(
    r"(?i)(<.*>|\$\{?\w+\}?|%\w+%|changeme|example|placeholder|your[-_ ]|xxxx|\*{3,}|dummy|"
    r"os\.environ|getenv|redacted|secrets?\.)"
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    preview: str


def _files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard"], capture_output=True, check=True)  # fmt: skip
        names = [n for n in out.stdout.decode("utf-8", "replace").split("\0") if n]
        return [root / n for n in names]
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_file() and not any(part in SKIP_DIRS for part in rel.parts[:-1]):
            found.append(p)
    return found


def _mask(text: str) -> str:
    return text[:4] + "…" if len(text) > 4 else "…"


def scan(root: Path) -> list[Finding]:
    root = Path(root)
    findings: list[Finding] = []
    for path in sorted(_files(root)):
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if BAD_NAMES.search(rel) and not ALLOWED_NAMES.search(rel):
            findings.append(
                Finding(rel, 0, "file that should not be committed", path.name[:4] + "…")
            )
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if "nosecret" in line:
                continue
            for rule, rx in PATTERNS.items():
                m = rx.search(line)
                if not m:
                    continue
                value = m.group(1) if rule == "literal secret" else m.group(0)
                if rule == "literal secret" and _PLACEHOLDER.search(value):
                    continue
                findings.append(Finding(rel, n, rule, _mask(value)))
    return findings
