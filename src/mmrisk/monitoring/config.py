"""Thresholds for the prediction-store checks. Every number here is an ASSUMPTION, not a finding.

They are starting points for a synthetic simulation. A real deployment would set them from its own
history and from what the review team can handle.
"""

from __future__ import annotations

# Windows
DEFAULT_WINDOW = "1d"  # polars duration: 1h, 6h, 1d
MIN_ROWS_FOR_SHARE_TEST = 100  # fewer rows in a window: its share is not tested
MIN_EXPECTED_FLAGGED = 5.0  # n * baseline share must reach this, or the test has no power

# A share (flagged, blocked) is compared with the baseline using a z score of the difference.
Z_WARN = 3.0
Z_FAIL = 5.0

# One window against the baseline: fraud arrives in clusters (the same customers are flagged
# together), so shares vary more from window to window than a plain binomial allows. Two healthy
# weeks of the default synthetic data gave a largest |z| of 2.8 (daily windows) to 4.1 (6 h
# windows), so a single window only warns, and only from this value up. It never fails a run.
WINDOW_Z_WARN = 5.0

# Volume per hour relative to the baseline's: outside [1/VOLUME_FACTOR, VOLUME_FACTOR] warns.
VOLUME_FACTOR = 2.0

# How old the newest scored transaction may be before the store counts as stale (needs --as-of).
STALE_AFTER_HOURS = 6.0

ACTION_FOR_LEVEL = {"LOW": "ALLOW", "MEDIUM": "REVIEW", "HIGH": "BLOCK"}
