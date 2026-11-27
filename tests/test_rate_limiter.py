"""
QDE V1.1 - tests/test_rate_limiter.py

Regression tests for utils/rate_limiter.py. Added after a CRITICAL
production bug: the Bulk API revision introduced acquire(weight=40.0)
calls, but the bucket's default capacity was only 8 (derived from
requests_per_second=8.0), so those requests could never be satisfied
and the scan hung indefinitely right after symbol discovery - observed
directly in a real, hours-long-stuck Termux session.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from utils.rate_limiter import RateLimiter, _MIN_CAPACITY_FLOOR


def test_default_capacity_meets_the_floor():
    limiter = RateLimiter(requests_per_second=8.0)
    assert limiter.capacity >= _MIN_CAPACITY_FLOOR


def test_explicit_burst_overrides_the_floor():
    limiter = RateLimiter(requests_per_second=8.0, burst=100)
    assert limiter.capacity == 100


async def test_bulk_endpoint_weights_are_satisfiable_without_hanging():
    """The exact regression: weight=40.0 (get_all_tickers /
    get_all_funding_rates) and weight=10.0 (get_all_mark_prices) must
    complete quickly against a freshly-constructed, fully-charged
    bucket - not hang forever.
    """
    limiter = RateLimiter(requests_per_second=8.0, burst=50)

    start = time.monotonic()
    await asyncio.wait_for(limiter.acquire(weight=40.0), timeout=2.0)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0

    await asyncio.wait_for(limiter.acquire(weight=10.0), timeout=2.0)


async def test_weight_exceeding_capacity_raises_immediately_not_hangs():
    """Critical fix: a request whose weight can never be satisfied
    (because it exceeds the bucket's total capacity) must fail fast
    with a clear error, not spin in acquire()'s while-loop forever.
    """
    limiter = RateLimiter(requests_per_second=8.0, burst=50)

    with pytest.raises(ValueError, match="exceeds bucket capacity"):
        await asyncio.wait_for(limiter.acquire(weight=999.0), timeout=2.0)


def test_config_from_json_reads_burst_field():
    config = {"requests_per_second": 8.0, "max_retries": 3, "burst": 50}
    limiter = RateLimiter.from_config(config)
    assert limiter.capacity == 50


def test_config_from_json_without_burst_uses_floor():
    config = {"requests_per_second": 8.0, "max_retries": 3}
    limiter = RateLimiter.from_config(config)
    assert limiter.capacity >= _MIN_CAPACITY_FLOOR


def test_real_config_file_has_sufficient_burst_for_all_actual_weights():
    """Guards against this exact class of bug recurring: every weight
    value actually used in data/exchange.py must be <= the burst
    configured in config/config.json - checked directly against the
    real config file, not a hardcoded assumption.
    """
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "config" / "config.json").read_text())
    configured_burst = config["rate_limit"].get("burst", _MIN_CAPACITY_FLOOR)

    exchange_source = (root / "data" / "exchange.py").read_text()
    weights_used = [float(w) for w in re.findall(r"weight=(\d+(?:\.\d+)?)", exchange_source)]

    assert weights_used, "no weight= usages found - test itself may be out of date"
    max_weight_used = max(weights_used)
    assert configured_burst >= max_weight_used, (
        f"config burst ({configured_burst}) is smaller than the largest weight actually "
        f"used in data/exchange.py ({max_weight_used}) - this exact mismatch caused the "
        f"infinite-hang production bug this test suite now guards against"
    )
