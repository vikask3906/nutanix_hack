"""Retry backoff. Pure Python - no Django, no clock, no database.

The delay before attempt N+1, given the retry block on a step::

    {"max": 5, "backoff": "exponential", "base_ms": 500, "max_ms": 60000}

Three strategies, all capped, all jittered.

WHY JITTER IS NOT OPTIONAL
--------------------------
An upstream service falls over. Two hundred steps fail within the same second.
Without jitter every one of them computes the identical delay and retries in
lockstep - so the service comes back up, gets hit by two hundred simultaneous
requests, and falls over again. The retry storm keeps it down.

With full jitter each retry lands at a uniformly random point in [0, delay], so
the same two hundred requests spread smoothly across the window. This is not a
micro-optimisation; it is the difference between a service recovering and a
service being held down by its own clients.

We use "full jitter" (uniform over the whole interval) rather than "equal jitter"
(half fixed, half random) because it spreads load best when many clients share a
failure, which is exactly our case.
"""

import random

DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_BASE_MS = 500
DEFAULT_MAX_MS = 60_000
DEFAULT_BACKOFF = "exponential"


def max_attempts(step):
    """How many times this step may run in total. 1 means no retry."""
    retry = step.get("retry")
    if not retry:
        return DEFAULT_MAX_ATTEMPTS
    return max(1, int(retry.get("max", DEFAULT_MAX_ATTEMPTS)))


def base_delay_ms(attempt, retry):
    """The un-jittered delay in milliseconds before the given attempt number.

    ``attempt`` is the attempt that just FAILED, so the first failure is 1.
    """
    retry = retry or {}
    strategy = retry.get("backoff", DEFAULT_BACKOFF)
    base = int(retry.get("base_ms", DEFAULT_BASE_MS))
    ceiling = int(retry.get("max_ms", DEFAULT_MAX_MS))

    if strategy == "fixed":
        delay = base
    elif strategy == "linear":
        delay = base * attempt
    else:  # exponential
        # Guard the exponent: attempt 40 would otherwise compute a number with
        # more digits than the age of the universe in milliseconds before the
        # cap is applied.
        delay = base * (2 ** min(attempt - 1, 30))

    return min(delay, ceiling)


def delay_seconds(attempt, retry, rng=None):
    """Jittered delay in seconds before retrying after ``attempt`` failed."""
    rng = rng or random
    base = base_delay_ms(attempt, retry)
    jittered = rng.uniform(0, base)
    return jittered / 1000.0


def should_retry(step, attempt):
    """True if a step that just failed on ``attempt`` has attempts remaining."""
    return attempt < max_attempts(step)
