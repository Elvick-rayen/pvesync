"""Exponential backoff with a ceiling and a little jitter."""

from __future__ import annotations

import random


def backoff_delay(attempt: int, *, initial: float, maximum: float, multiplier: float = 2.0,
                  jitter: float = 0.1, rng: random.Random | None = None) -> float:
    """Delay before retry number ``attempt`` (1 = first retry).

    ``initial * multiplier**(attempt-1)``, capped at ``maximum``, then +/- ``jitter``
    (fraction) so many agents do not retry against NetBox at the same instant.
    With the defaults (5 s, x2, max 1 h): 5 s, 10 s, 20 s, 40 s, ... 1 h, 1 h, ...
    """
    attempt = max(1, attempt)
    delay = min(maximum, initial * (multiplier ** min(attempt - 1, 60)))
    if jitter:
        r = rng.random() if rng else random.random()  # noqa: S311 - jitter, not crypto
        delay *= 1 + jitter * (2 * r - 1)
    return max(0.0, min(delay, maximum * (1 + jitter)))
