"""Evidence scorecard (Phase 27): each area of the project gets a level that is computed from
evidence a reader can open, never chosen by hand.

The data is `docs/scorecard.json`; `python -m mmrisk.scorecard render` writes `docs/scorecard.md`
and `check` fails if the data cites something that does not exist or the page is out of date.
"""
