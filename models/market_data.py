"""
QDE V1 - models/market_data.py

The unified market data object (Document 02 SS14, Document 06 SS7).

This is the single object that flows from data.collector into every
indicator and into engines.hard_filter. No indicator or engine ever
requests data from the exchange directly - everything reads from this
object.

Dependency level: 0 (no internal imports). Pure data holders only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OHLCVCandle:
    """A single OHLCV candle."""

    timestamp: int  # epoch milliseconds, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float

    def is_valid(self) -> bool:
        """Basic sanity check used by Hard Filter Rule 9 (corrupted data)."""
        values = (self.open, self.high, self.low, self.close, self.volume)
        if any(v is None for v in values):
            return False
        if any(v != v for v in values):  # NaN check without importing math
            return False
        if self.high < self.low:
            return False
        return True


@dataclass(frozen=True)
class TimeframeData:
    """OHLCV history for a single timeframe (e.g. '15m', '1h')."""

    timeframe: str
    candles: list[OHLCVCandle] = field(default_factory=list)

    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    def highs(self) -> list[float]:
        return [c.high for c in self.candles]

    def lows(self) -> list[float]:
        return [c.low for c in self.candles]

    def volumes(self) -> list[float]:
        return [c.volume for c in self.candles]

    def is_sufficient(self, min_candles: int) -> bool:
        """Used by Hard Filter Rule 3 (missing candle history)."""
        return len(self.candles) >= min_candles and all(
            c.is_valid() for c in self.candles
        )


@dataclass
class MarketData:
    """Unified per-symbol market snapshot (Document 02 SS14).

    Assembled once per symbol per scan by data.collector.DataCollector
    and shared read-only by every downstream consumer.
    """

    symbol: str
    timeframes: dict[str, TimeframeData]
    funding: dict[str, Any]
    open_interest: dict[str, Any]
    ticker: dict[str, Any]
    mark_price: dict[str, Any]
    timestamp: str  # UTC ISO8601

    def has_timeframe(self, timeframe: str) -> bool:
        return timeframe in self.timeframes and bool(
            self.timeframes[timeframe].candles
        )

    def get_timeframe(self, timeframe: str) -> TimeframeData | None:
        return self.timeframes.get(timeframe)

    def get_closes(self, timeframe: str) -> list[float]:
        tf = self.get_timeframe(timeframe)
        return tf.closes() if tf else []

    def get_highs(self, timeframe: str) -> list[float]:
        tf = self.get_timeframe(timeframe)
        return tf.highs() if tf else []

    def get_lows(self, timeframe: str) -> list[float]:
        tf = self.get_timeframe(timeframe)
        return tf.lows() if tf else []

    def get_volumes(self, timeframe: str) -> list[float]:
        tf = self.get_timeframe(timeframe)
        return tf.volumes() if tf else []

    def has_funding(self) -> bool:
        """Used by Hard Filter Rule 5 (missing funding data)."""
        return bool(self.funding)

    def has_open_interest(self) -> bool:
        """Used by Hard Filter Rule 4 (missing open interest)."""
        return bool(self.open_interest)

    def quote_volume(self) -> float | None:
        """Used by Hard Filter Rule 6 (daily quote volume threshold)."""
        return self.ticker.get("quoteVolume")

    def spread(self) -> float | None:
        """Used by Hard Filter Rule 7 (spread threshold).

        Expected to be pre-computed by data.collector as
        (ask - bid) / mid_price and stored in ticker['spread'].
        """
        return self.ticker.get("spread")

    def is_minimally_valid(self, required_candles: dict[str, int]) -> bool:
        """Aggregate validity check combining Hard Filter Rules 3-5, 9.

        required_candles maps timeframe -> minimum candle count
        (e.g. {'15m': 200, '1h': 250, '4h': 150, '1d': 100}).
        """
        if not self.has_funding() or not self.has_open_interest():
            return False
        for tf, minimum in required_candles.items():
            timeframe_data = self.get_timeframe(tf)
            if timeframe_data is None or not timeframe_data.is_sufficient(minimum):
                return False
        return True
