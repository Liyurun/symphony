"""Tests for retry handler."""

import pytest
from symphony.sop.sop_definition import NodeRetry, RetryStrategy
from symphony.sop.retry import RetryHandler


class TestRetryHandler:
    def setup_method(self):
        self.handler = RetryHandler()

    def test_fixed_delay(self):
        config = NodeRetry(max_attempts=3, backoff=RetryStrategy.FIXED, initial_delay=2.0)
        assert self.handler.calc_delay(config, 1) == 2.0
        assert self.handler.calc_delay(config, 2) == 2.0
        assert self.handler.calc_delay(config, 3) == 2.0

    def test_linear_delay(self):
        config = NodeRetry(max_attempts=3, backoff=RetryStrategy.LINEAR, initial_delay=1.0)
        assert self.handler.calc_delay(config, 1) == 1.0
        assert self.handler.calc_delay(config, 2) == 2.0
        assert self.handler.calc_delay(config, 3) == 3.0

    def test_exponential_delay(self):
        config = NodeRetry(max_attempts=3, backoff=RetryStrategy.EXPONENTIAL, initial_delay=1.0)
        # attempt 1: 1.0 * 2^0 = 1.0 (with ±25% jitter)
        delay1 = self.handler.calc_delay(config, 1)
        assert 0.75 <= delay1 <= 1.25
        # attempt 2: 1.0 * 2^1 = 2.0 (with ±25% jitter)
        delay2 = self.handler.calc_delay(config, 2)
        assert 1.5 <= delay2 <= 2.5
        # attempt 3: 1.0 * 2^2 = 4.0 (with ±25% jitter)
        delay3 = self.handler.calc_delay(config, 3)
        assert 3.0 <= delay3 <= 5.0
