# ADR-006: Installable `src/mmrisk` package
**Context.** The initial plan had loose folders under `src/`. Imports from tests, training scripts and the API would then depend on the working directory.
**Decision.** Use one installable package, `src/mmrisk/` (subpackages: ingestion, validation, features, models, inference, monitoring, utils), installed with `pip install -e .`. Top-level `training/` and `apps/` hold entry points that import from `mmrisk`. Runtime dependencies are added phase by phase.
**Alternatives.** Loose `src/` folders with path hacks (fragile); a single flat package (less clear boundaries).
**Consequences.** The same imports work locally, in pytest and in CI, and the shared feature library lives in one place for training and serving. Small cost: an editable install step.
