"""smartapi_connector.rate_limit

Simple in-memory token-bucket rate limiter suitable for RESTClient.

- Per-endpoint configuration via ConnectorConfig.rate_limit_overrides()
- acquire(endpoint_name) returns True if a token is available, False otherwise
- refill is time-based; implementation is process-local and not distributed

This module is intentionally simple and testable. It will be used by RESTClient
and can be extended/replaced by a distributed limiter if required.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Dict, Optional, Any

from .config import get_config


class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = float(rate)  # tokens per second
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed <= 0:
            return
        added = elapsed * self.rate
        self._tokens = min(self.capacity, self._tokens + added)
        self._last = now

    def consume(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


class RateLimiter:
    def __init__(self, config: Optional[Any] = None):
        self._config = config or get_config()
        self._buckets: Dict[str, TokenBucket] = {}
        self._default_rate = 5.0  # tokens per second default
        self._default_capacity = 5.0
        self._init_buckets_from_config()

    def _init_buckets_from_config(self) -> None:
        overrides = {}
        try:
            overrides = self._config.rate_limit_overrides
        except Exception:
            overrides = {}

        for endpoint, spec in overrides.items():
            try:
                rps = float(spec.get("rps", self._default_rate))
                burst = float(spec.get("burst", max(1, rps)))
                self._buckets[endpoint] = TokenBucket(rate=rps, capacity=burst)
            except Exception:
                continue

    def acquire(self, endpoint_name: Optional[str]) -> bool:
        """Attempt to acquire one token for the endpoint. If endpoint is None or unknown,
        use a default global bucket.
        """
        if endpoint_name and endpoint_name in self._buckets:
            return self._buckets[endpoint_name].consume(1.0)

        # fallback global behavior
        # lazily create global bucket
        if "__global__" not in self._buckets:
            self._buckets["__global__"] = TokenBucket(rate=self._default_rate, capacity=self._default_capacity)
        return self._buckets["__global__"].consume(1.0)


# Simple factory for tests and consumers
def create_rate_limiter(config: Optional[Any] = None) -> RateLimiter:
    return RateLimiter(config=config)
