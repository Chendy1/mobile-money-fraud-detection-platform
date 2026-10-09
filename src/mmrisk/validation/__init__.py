"""Data quality: row-level domain rules (applied during ingestion) and dataset checks.

Kept import-free on purpose: `mmrisk.ingestion` imports `mmrisk.validation.rules`, and
`mmrisk.validation.checks` imports `mmrisk.ingestion`, so importing either here would
create a cycle.
"""
