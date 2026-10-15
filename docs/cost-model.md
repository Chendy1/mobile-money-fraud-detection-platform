# Cost model (assumed)

> **Every number on this page is an assumption for a simulation.** None comes from Safaricom, M-Pesa or any provider. Change them to see how decisions and savings move; do not read the results as real money.

## What the model decides
For each transaction the model gives a calibrated fraud probability `p`; the amount is `a`. Three actions are compared by expected cost:

| Action | Risk level | Expected cost |
|---|---|---|
| ALLOW | LOW | `p x loss_rate x a` |
| REVIEW | MEDIUM | `review_cost + p x (1 - review_prevents) x loss_rate x a` |
| BLOCK | HIGH | `(1 - p) x block_cost` |

The action with the lowest expected cost is chosen; a tie goes to the less severe action. Labels are never used when deciding. Because the loss grows with the amount, the same probability can mean ALLOW for a small transaction and REVIEW for a large one.

## Assumptions
| Parameter | Default | Meaning | CLI flag |
|---|---|---|---|
| `loss_rate` | 1.0 | Share of a fraudulent amount lost if it goes through (1 = nothing recovered) | `--loss-rate` |
| `review_cost` | 25 KES | Cost of every review (analyst time, a verification message) | `--review-cost` |
| `review_prevents` | 0.7 | Share of fraud a review stops | `--review-prevents` |
| `block_cost` | 150 KES | Friction when a legitimate transaction is blocked (a blocked fraud costs nothing here) | `--block-cost` |

Invalid values (a negative cost, a share outside 0 to 1) stop the run with exit code 2.

## How to try other values
```powershell
.\.venv\Scripts\python -m mmrisk.models advanced --review-cost 50 --block-cost 300 --review-prevents 0.5
```
The report always includes a sensitivity table (review and block costs at 0.25x to 4x, review effectiveness at 50%, 70%, 90%) so you can see whether the ranking of scorers depends on the assumption. In the committed report it does.

## What the model leaves out
- Review queues, delays and analyst capacity.
- Customers who leave after being blocked, and the reputational cost of a wrong block.
- Reversals, chargebacks, regulatory costs and fraud limits.
- Partial recovery that depends on the fraud pattern.
- The label noise: costs are computed from observed labels, which miss some fraud and include a few false reports. The optional audit section repeats the calculation with the hidden truth.

Code: `src/mmrisk/models/costs.py`. Decision: [ADR-013](adr/ADR-013-calibration-and-cost-based-decisions.md).
