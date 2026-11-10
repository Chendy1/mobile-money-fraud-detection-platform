"""Cost estimate for the AWS design, from prices read on AWS pages (Phase 23).

No number in this package is invented: every rate comes from `infrastructure/cost/prices.json`
with its source and the date it was read; the assumptions about use (hours, sizes) are named and
labelled ASSUMED; what is not priced is listed with every result. It estimates; it quotes nothing.
"""

from mmrisk.cloudcost.model import (
    Assumptions,
    CostError,
    Estimate,
    Line,
    estimate,
    load_prices,
)

__all__ = ["Assumptions", "CostError", "Estimate", "Line", "estimate", "load_prices"]
