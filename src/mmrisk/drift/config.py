"""Thresholds and per-feature policy for drift detection. All numbers are ASSUMPTIONS.

PSI limits 0.1 and 0.25 are the usual rules of thumb from credit scoring, not laws. Measured on
the default synthetic data (see ADR-020): features that did not change sit at the noise level
(PSI below 0.01 on a week of data against a 45-day reference), and two kinds of feature move
for reasons that are not a change in the world:
  calendar     is_salary_window is 1 on days 25 to 3 of each month, and day_of_week repeats every
               seven days, so a window shorter than the cycle has a different mix than a reference
               that spans it. They are not scored for windows shorter than their cycle
               (CALENDAR_MIN_DAYS).
  accumulating n_prior_txns and account_age_days grow as time passes, by construction. They are
               scored and shown, but do not by themselves declare drift.
"""

from __future__ import annotations

PSI_WARN = 0.10  # moderate shift
PSI_FAIL = 0.25  # major shift
MIN_CURRENT_ROWS = 500  # fewer rows in the current window: nothing is scored
CALENDAR_MIN_DAYS = {"is_salary_window": 31.0, "day_of_week": 7.0}  # the cycle, in days
ACCUMULATING = frozenset({"n_prior_txns", "account_age_days"})

DEFAULT_LAST_DAYS = 7.0
DEFAULT_SERIES_WINDOW = "1d"
