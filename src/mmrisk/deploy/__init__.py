"""A production-like local deployment (Phase 20).

Production-like means: keys, TLS, secrets in files that are not in Git, hardened containers that
restart, health checks, backups, and a preflight that refuses to start on a bad setup. It does not
mean production: it runs on one computer, nothing here has been exposed to a network, and the
container pieces are checked as files only (TO VERIFY by running them).
"""

from __future__ import annotations


class DeployError(ValueError):
    """The deployment cannot be set up or checked as asked."""
