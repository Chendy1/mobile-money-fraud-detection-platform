"""One result type and the small helpers the preflight and the verification share."""

from __future__ import annotations

from dataclasses import dataclass

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""

    def line(self) -> str:
        return f"  {self.status.upper():4}  {self.name}" + (
            f": {self.detail}" if self.detail else ""
        )


def ok(name: str, detail: str = "") -> Check:
    return Check(name, PASS, detail)


def warn(name: str, detail: str = "") -> Check:
    return Check(name, WARN, detail)


def fail(name: str, detail: str = "") -> Check:
    return Check(name, FAIL, detail)


def verdict(checks: list[Check], strict: bool = False) -> int:
    """0 when nothing failed (and, in strict mode, nothing warned); 1 otherwise."""
    bad = {FAIL, WARN} if strict else {FAIL}
    return 1 if any(c.status in bad for c in checks) else 0


def summary(checks: list[Check]) -> str:
    n = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL)}
    return f"{n[PASS]} passed, {n[WARN]} warnings, {n[FAIL]} failed"
