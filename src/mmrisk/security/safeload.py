"""Load a saved model bundle without letting the file run arbitrary code.

A joblib file is a pickle, and unpickling can execute anything the file says. This loader reads it
with an allow-list: only the classes this project saves (and numpy's array rebuilding helpers) may
be looked up; any other global reference (os.system, subprocess, eval, builtins.exec ...) stops the
load with an error that names it.

What this is not: a sandbox. The allowed classes run their own `__setstate__` code, and a
determined attacker who can write a file you load might still find a way through them. It removes
the easy attack (a file that calls os.system) and it fails closed: if the installed joblib is not
one this was written for, loading is refused instead of falling back to the unsafe path.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from joblib.numpy_pickle import NumpyUnpickler

ALLOWED = frozenset(
    {
        ("mmrisk.models.bundle", "RiskModel"),
        ("mmrisk.models.boosting", "LGBMModel"),
        ("mmrisk.models.calibration", "PlattCalibrator"),
        ("mmrisk.models.costs", "CostModel"),
        # numpy rebuilding helpers (names moved between numpy 1.x and 2.x)
        ("numpy", "dtype"),
        ("numpy", "ndarray"),
        ("numpy.core.multiarray", "_reconstruct"),
        ("numpy._core.multiarray", "_reconstruct"),
        ("numpy.core.multiarray", "scalar"),
        ("numpy._core.multiarray", "scalar"),
    }
)


class UnsafePickleError(ValueError):
    """The file refers to something outside the allow-list, or cannot be checked."""


class _Restricted(NumpyUnpickler):
    def find_class(self, module: str, name: str):
        if (module, name) not in ALLOWED:
            raise UnsafePickleError(
                f"the model file refers to {module}.{name}, which is not on the allow-list; "
                "refusing to load it"
            )
        return super().find_class(module, name)


def safe_load(path: Path):
    path = Path(path)
    params = inspect.signature(NumpyUnpickler.__init__).parameters
    if "filename" not in params or "file_handle" not in params:
        raise UnsafePickleError("the installed joblib cannot be used for a checked load")
    kwargs = {"mmap_mode": None}
    if "ensure_native_byte_order" in params:
        kwargs["ensure_native_byte_order"] = True  # what joblib.load does without mmap
    with open(path, "rb") as fh:
        if fh.read(1) != b"\x80":  # a plain pickle; compressed or foreign files are refused
            raise UnsafePickleError(f"{path} is not a plain joblib pickle")
        fh.seek(0)
        return _Restricted(str(path), fh, **kwargs).load()
