"""Load testing the scoring API (Phase 18 left it secured; Phase 19 measures it).

Open-loop: requests are scheduled at fixed times whatever the server does, and latency is measured
from the time a request was *meant* to start, so a slow server cannot hide its own queueing
(coordinated omission). Standard library only. Numbers describe the machine they were measured on.
"""
