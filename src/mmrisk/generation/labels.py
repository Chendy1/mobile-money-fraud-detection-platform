"""Observed labels: noisy and delayed, kept separate from the ground truth."""

from __future__ import annotations

import numpy as np

from mmrisk.generation.config import (
    DETECTION_RATE,
    DISPUTE_WINDOW_DAYS,
    FALSE_REPORT_RATE,
)


def make_labels(true_fraud: np.ndarray, t_epoch: np.ndarray, rng) -> tuple[np.ndarray, np.ndarray]:
    """Return (label_is_fraud, label_available_epoch).

    - Only ~DETECTION_RATE of true fraud is ever reported; the rest looks legitimate.
    - A tiny share of legitimate transactions is wrongly reported as fraud.
    - Reported fraud labels arrive after a delay (median ~3 days, up to 45 days).
    - Non-fraud labels are only "confirmed" after the dispute window.
    """
    m = len(true_fraud)
    reported = true_fraud & (rng.random(m) < DETECTION_RATE)
    wrongly = ~true_fraud & (rng.random(m) < FALSE_REPORT_RATE)
    label = reported | wrongly
    delay_days = np.clip(np.exp(rng.normal(np.log(3.0), 0.9, m)), 0.25, 45.0)
    delay = np.where(label, delay_days, float(DISPUTE_WINDOW_DAYS)) * 86400
    return label.astype(np.int8), t_epoch + delay.astype(np.int64)
