# ADR-004: DuckDB + Parquet
**Context.** Need analytics, a layered lake, and fast feature computation without running servers.
**Decision.** Parquet files as the lake, DuckDB for SQL analytics, Polars for transformations. A relational store (PostgreSQL) is used for predictions only once Docker is introduced.
**Alternatives.** PostgreSQL for everything (heavier for analytics); a cloud warehouse (costs money).
**Consequences.** Zero servers, portable, reproducible. Not for multi-writer concurrency, which is acceptable for a simulation.
