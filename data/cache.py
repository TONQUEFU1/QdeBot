"""
QDE V1 - data/cache.py

Market data cache (Document 02 SS9, Document 07 SS13).

Architecture:
    Binance -> Cache -> Indicators

Ensures identical data (e.g. 15m candles for ETHUSDT) is downloaded
once and reused by every indicator that needs it, and across cache
levels with different TTLs (exchange info rarely changes, OHLCV
changes every scan, indicator results are scan-scoped).

Dependency level: 0 (no internal imports).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class MarketCache:
    """In-memory TTL cache keyed by (symbol, data_type, timeframe).

    TTLs are configured per data_type via `ttl_config`
    (Document 09 SS8: config.json -> cache.ttl_seconds), e.g.:
        {"exchange_info": 3600, "ohlcv": 60, "ticker": 30}

    A data_type with no configured TTL defaults to `default_ttl`
    seconds (one full scan's worth), so results are safely reused
    within a single scan even if not explicitly configured.
    """

    def __init__(self, ttl_config: dict[str, int], default_ttl: int = 120) -> None:
        self._ttl_config = ttl_config
        self._default_ttl = default_ttl
        self._store: dict[tuple[str, str, str | None], _CacheEntry] = {}

    def _key(self, symbol: str, data_type: str, timeframe: str | None) -> tuple[str, str, str | None]:
        return (symbol, data_type, timeframe)

    def _ttl_for(self, data_type: str) -> int:
        return self._ttl_config.get(data_type, self._default_ttl)

    def get(self, symbol: str, data_type: str, timeframe: str | None = None) -> Any | None:
        """Return the cached value, or None if missing/expired."""
        key = self._key(symbol, data_type, timeframe)
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.monotonic() >= entry.expires_at:
            del self._store[key]
            return None
        return entry.value

    def set(self, symbol: str, data_type: str, value: Any, timeframe: str | None = None) -> None:
        key = self._key(symbol, data_type, timeframe)
        ttl = self._ttl_for(data_type)
        self._store[key] = _CacheEntry(value=value, expires_at=time.monotonic() + ttl)

    def has(self, symbol: str, data_type: str, timeframe: str | None = None) -> bool:
        return self.get(symbol, data_type, timeframe) is not None

    def clear(self) -> None:
        """Wipe the entire cache. Useful between independent scan runs
        if stale data must not leak across scans.
        """
        self._store.clear()

    def size(self) -> int:
        return len(self._store)
