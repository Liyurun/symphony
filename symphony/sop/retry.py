"""Retry handler — implements backoff strategies for SOP node retries."""

from __future__ import annotations

import random

from symphony.sop.sop_definition import NodeRetry, RetryStrategy


class RetryHandler:
    """Handles retry delays for SOP nodes.

    Strategies:
    - fixed: constant delay between attempts
    - linear: delay increases linearly with attempt count
    - exponential: delay doubles each attempt (with jitter)
    """

    def calc_delay(self, retry_config: NodeRetry, attempt: int) -> float:
        """Calculate the delay before the next retry attempt.

        Args:
            retry_config: The node's retry configuration.
            attempt: Current attempt number (1-based).

        Returns:
            Delay in seconds.
        """
        base = retry_config.initial_delay

        if retry_config.backoff == RetryStrategy.FIXED:
            return base

        elif retry_config.backoff == RetryStrategy.LINEAR:
            return base * attempt

        elif retry_config.backoff == RetryStrategy.EXPONENTIAL:
            # Exponential backoff with jitter (±25%)
            delay = base * (2 ** (attempt - 1))
            jitter = delay * 0.25 * (random.random() * 2 - 1)
            return max(base, delay + jitter)

        return base
