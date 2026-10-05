# ADR-007: Separate observable data, observed labels and ground truth
**Context.** In a synthetic dataset the generator knows the true fraud pattern, which makes it easy to leak that knowledge into features. Real systems also learn labels late and imperfectly.
**Decision.** Write three layers: observable tables (no fraud-related columns), `labels.parquet` (noisy, delayed, with `label_available_at`), and `ground_truth/` (true flags, patterns, latent attributes) reserved for analysis and tests. Device ids are randomly permuted so numbering carries no information. Tests enforce column hygiene, label noise, signal overlap and id leakage.
**Alternatives.** One wide table with an `is_fraud` column (simple, but leaks and hides label delay); perfectly accurate labels (unrealistic, hides the retraining problem).
**Consequences.** Training must join labels by availability time (point-in-time correctness, Phase 6). Slightly more files and joins. Leakage becomes testable instead of assumed away.
