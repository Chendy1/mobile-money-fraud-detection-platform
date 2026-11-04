"""The dashboard login, as plain logic (no Streamlit), so it can be tested.

One shared password, checked against a scrypt hash. Failed attempts are counted for the whole
dashboard (Streamlit does not tell the script a client address): after LIMIT failures in WINDOW
seconds every attempt is refused until the window passes. That also locks the real user out for
a while, which is the price of not trusting a client address. ASSUMED values, not tuned.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from mmrisk.security import passwords
from mmrisk.security.ratelimit import FailureLimiter

LIMIT, WINDOW = 5, 300.0  # ASSUMED
KEY = "dashboard"


class Gate:
    def __init__(self, stored_hash: str, clock: Callable[[], float] = time.monotonic):
        passwords.parse(stored_hash)  # a bad hash fails at start, not at the first login
        self._hash = stored_hash
        self._limiter = FailureLimiter(LIMIT, WINDOW, clock)

    def attempt(self, password: str) -> tuple[bool, str]:
        if self._limiter.blocked(KEY):
            wait = int(self._limiter.retry_after(KEY)) + 1
            return False, f"too many wrong passwords; try again in {wait} s"
        if passwords.verify(password, self._hash):
            self._limiter.reset(KEY)
            return True, ""
        self._limiter.fail(KEY)
        return False, "wrong password"
