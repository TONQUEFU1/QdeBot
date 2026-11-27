"""
QDE V1 - utils/rate_limiter.py

Async token-bucket rate limiter (Document 02 SS10, Document 07 SS15).
Used exclusively by data.exchange.ExchangeClient to avoid flooding
Binance Futures endpoints.

V1.1 CRITICAL FIX (V1 Final Audit revision): the Bulk API revision
introduced acquire() calls with weight=40.0 (get_all_tickers,
get_all_funding_rates) and weight=10.0 (get_all_mark_prices), but the
bucket's default capacity (derived from requests_per_second=8.0 in
config.json) was only 8 tokens - since _tokens can never exceed
`capacity`, a request for weight=40 could NEVER be satisfied, and the
while-loop in acquire() spun forever, sleeping repeatedly, hanging the
scan indefinitely right after symbol discovery (confirmed against a
real hung Termux session). Fixed by:
    1. Raising immediately if a single request's weight exceeds the
       bucket's total capacity - this can never succeed no matter how
       long the caller waits, so silently hanging is the wrong
       behavior; a clear, immediate error is correct and would have
       caught this bug in seconds instead of hours.
    2. Raising the default capacity floor so realistic bulk-endpoint
       weights are satisfiable out of the box, and letting
       from_config() read an explicit `burst` override for tuning.

Dependency level: 0 (no internal imports).
"""

from __future__ import annotations

import asyncio
import time

# Floor for the token bucket's capacity when no explicit `burst` is
# configured. Must be >= the largest single-request weight anywhere in
# data/exchange.py (currently 40.0 for the bulk ticker/funding
# endpoints) or acquire() can never succeed for that call - see the
# CRITICAL FIX note above.
_MIN_CAPACITY_FLOOR = 50


class RateLimiter:
    """Async token-bucket rate limiter.

    Tokens refill continuously at `requests_per_second`. Callers await
    `acquire()` before making a request; it blocks only long enough to
    respect the configured rate.

    Example:
        limiter = RateLimiter(requests_per_second=10.0, burst=50)
        async with limiter:
            await client.get(...)
    """

    def __init__(self, requests_per_second: float, burst: int | None = None) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.rate = requests_per_second
        self.capacity = burst if burst is not None else max(_MIN_CAPACITY_FLOOR, int(requests_per_second))
        self._tokens = float(self.capacity)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: float = 1.0) -> None:
        """Block until `weight` tokens are available, then consume them.

        `weight` allows heavier endpoints (e.g. exchange info) to cost
        more than a single OHLCV request, matching Binance's own
        weighted rate-limit model.

        Raises ValueError immediately if `weight` exceeds the bucket's
        total capacity - such a request can never be satisfied no
        matter how long it waits (see module-level CRITICAL FIX note),
        so failing fast is correct; the caller misconfigured a weight
        larger than the bucket was sized for.
        """
        if weight > self.capacity:
            raise ValueError(
                f"Requested weight {weight} exceeds bucket capacity {self.capacity} - "
                f"this request can never be satisfied. Increase RateLimiter's `burst` "
                f"(or config.json's rate_limit.burst) to at least {weight}."
            )
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                deficit = weight - self._tokens
                wait_time = deficit / self.rate
                await asyncio.sleep(wait_time)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated_at
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated_at = now

    async def __aenter__(self) -> "RateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    @classmethod
    def from_config(cls, config: dict) -> "RateLimiter":
        """Build from the `rate_limit` section of config.json
        (Document 09 SS8): {"requests_per_second": float, "max_retries": int,
        "burst": int (optional)}. Only requests_per_second/burst are
        consumed here; max_retries belongs to utils.retry.
        """
        return cls(
            requests_per_second=config["requests_per_second"],
            burst=config.get("burst"),
        )
