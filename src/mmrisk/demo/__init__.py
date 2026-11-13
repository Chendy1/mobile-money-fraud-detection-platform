"""Guided demo (Phase 25): runs the project's real commands in a fixed order, with the talking
points for each step and a check that the output says what the step is meant to show.

It runs nothing the earlier phases do not already provide, writes only under `data/demo/`, and
never prints a result it did not just read from a command's output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class DemoError(Exception):
    """A mistake in how the demo was asked for (exit code 2), not a failed step."""


@dataclass(frozen=True)
class Paths:
    """Where the demo reads from and writes to. Defaults match `dev.ps1`."""

    repo: Path = Path(".")
    lake: Path = Path("data/processed")
    model_dir: Path = Path("models/advanced")
    registry: Path = Path("models/registry")
    card: Path = Path("docs/model-card.md")
    prices: Path = Path("infrastructure/cost/prices.json")
    out: Path = Path("data/demo")

    def under_repo(self, p: Path) -> Path:
        return p if p.is_absolute() else self.repo / p


@dataclass(frozen=True)
class Step:
    """One thing to show. `commands` run as `python -m ...`; `action` names a built-in."""

    id: str
    title: str
    seconds: int  # measured in the assistant's sandbox, one run: a guide, not a promise
    say: tuple[str, ...]  # what to tell the audience, in plain words
    commands: tuple[tuple[str, ...], ...] = ()
    action: str = ""
    ok_codes: tuple[int, ...] = (0,)
    expect: tuple[str, ...] = ()  # regular expressions; every one must match the output
    needs: tuple[str, ...] = ()  # step ids that must have passed
    tracks: tuple[str, ...] = ("standard",)


@dataclass
class Result:
    id: str
    status: str  # ok | failed | unexpected | skipped
    seconds: float
    detail: str = ""
    output: str = ""
    lines: list[str] = field(default_factory=list)
