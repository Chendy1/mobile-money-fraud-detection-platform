# ADR-001: Synthetic data only
**Context.** Real mobile-money data is private, regulated, and unobtainable legitimately for a portfolio.
**Decision.** Generate all data synthetically with a seeded, configurable generator.
**Alternatives.** Public fraud datasets (different domain, not Kenyan mobile money); real data (not permitted).
**Consequences.** Fully reproducible and safe to publish. Risk: labels may leak generator rules, so we add label noise, latent variables, overlap and delayed labels, and test for leakage. Results prove engineering skill, not real-world performance.
