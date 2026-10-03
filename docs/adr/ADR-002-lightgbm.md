# ADR-002: LightGBM
**Context.** Tabular, imbalanced, time-ordered data on a 2-core, 8 GB machine.
**Decision.** Use LightGBM as the main model, benchmarked against rules, Logistic Regression and Random Forest.
**Alternatives.** XGBoost (comparable accuracy, heavier on memory here); deep learning (no benefit on tabular data of this size).
**Consequences.** Fast training, good SHAP support, low RAM. Needs care with categorical handling and calibration. Revisit if the benchmark disagrees.
