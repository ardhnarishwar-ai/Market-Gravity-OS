"""Retry policy for connector

Provides a pluggable retry policy with exponential backoff and optional jitter.
The execute(callable) method runs the callable and retries on TransientError or
RateLimitError. Sleep function is injectable for tests.
"""

from __future__ import annotations

import random
import time
from typing import Callable, Optional

from .errors import TransientError, RateLimitError


class RetryPolicy:
    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        backoff_factor: float = 2.0,
        max_delay: float = 60.0,
        jitter: float = 0.1,
        sleep_func: Optional[Callable[[float], None]] = None,
    ):
        self.max_attempts = int(max_attempts)
        self.base_delay = float(base_delay)
        self.backoff_factor = float(backoff_factor)
        self.max_delay = float(max_delay)
        self.jitter = float(jitter)
        self.sleep = sleep_func or time.sleep

    def _compute_delay(self, attempt: int) -> float:
        delay = min(self.max_delay, self.base_delay * (self.backoff_factor ** (attempt - 1)))
        if self.jitter and self.jitter > 0:
            delay = delay * (1.0 + (random.uniform(-self.jitter, self.jitter)))
        return delay

    def execute(self, fn: Callable[[], any]):
        last_exc = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except (TransientError, RateLimitError) as e:
                last_exc = e
                if attempt == self.max_attempts:
                    raise
                delay = self._compute_delay(attempt)
                try:
                    self.sleep(delay)
                except Exception:
                    pass
            except Exception:
                # Non-retryable
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("RetryPolicy failed without exception")
