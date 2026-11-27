"""
QDE V1 - engines/hard_filter.py

Hard Filter Engine (Document 02 SS15). Rejects invalid setups before
scoring. Rule 10 (API failure -> skip symbol) is handled upstream by
data.collector/main.py, since it occurs before a MarketData object
even exists.

V1.1 fix note (Document 10 diagnostic): Binance Futures' 24hr ticker
endpoint (what ccxt's fetch_ticker maps to for USDM futures) does not
return bid/ask fields, so market_data.spread() was always None for
every symbol - Rule 7 was therefore rejecting 100% of scanned symbols
regardless of actual quality. Rule 7 now only rejects when a spread
value IS available and exceeds the configured threshold; unavailable
spread is logged, not treated as a rejection. A proper fix (bulk
bookTicker fetch in data/exchange.py) is tracked separately and does
not require any change to this engine's interface.

Dependency level: 2 (depends on models.market_data, storage.logger).
"""

from __future__ import annotations

from typing import Any

from models.market_data import MarketData
from storage.logger import QDELogger


class HardFilterEngine:
    """Applies Document 02 SS15 Rules 1-9 sequentially. Rule 10 (API
    failure) is out of scope here - it never produces a MarketData to
    evaluate in the first place.
    """

    def __init__(self, filters: dict[str, Any]) -> None:
        self.min_quote_volume = filters.get("min_quote_volume", 0.0)
        self.max_spread = filters.get("max_spread", 1.0)
        self.min_liquidity_score = filters.get("min_liquidity_score", 0.0)
        self.required_candles = filters.get("required_candles", {})
        self.logger = QDELogger.get(__name__)

    def evaluate(self, market_data: MarketData) -> tuple[bool, list[str]]:
        """Returns (passed, reasons). `reasons` is empty when passed."""
        reasons: list[str] = []

        # Rule 3: missing candle history
        required = self.required_candles or {tf: 1 for tf in market_data.timeframes}
        for tf, minimum in required.items():
            tf_data = market_data.get_timeframe(tf)
            if tf_data is None or not tf_data.is_sufficient(minimum):
                reasons.append(f"missing_or_insufficient_candles:{tf}")

        # Rule 4: missing open interest
        if not market_data.has_open_interest():
            reasons.append("missing_open_interest")

        # Rule 5: missing funding data
        if not market_data.has_funding():
            reasons.append("missing_funding")

        # Rule 6: daily quote volume below threshold
        quote_volume = market_data.quote_volume()
        if quote_volume is None:
            reasons.append("missing_quote_volume")
        elif quote_volume < self.min_quote_volume:
            reasons.append(f"quote_volume_below_threshold:{quote_volume}")

        # Rule 7: spread above threshold. Spread is only checked when a
        # value is actually available - unavailability is a data-source
        # limitation, not a quality signal, and must not reject every
        # symbol (see module docstring).
        spread = market_data.spread()
        if spread is not None and spread > self.max_spread:
            reasons.append(f"spread_above_threshold:{spread}")
        elif spread is None:
            self.logger.debug(f"{market_data.symbol}: spread unavailable from data source (not rejected)")

        # Rule 9: corrupted data (NaN/null/missing values in candles)
        for tf, tf_data in market_data.timeframes.items():
            if tf_data.candles and not all(c.is_valid() for c in tf_data.candles):
                reasons.append(f"corrupted_candle_data:{tf}")

        passed = len(reasons) == 0
        if not passed:
            self.logger.debug(f"Hard filter rejection: {market_data.symbol} -> {reasons}")

        return passed, reasons

    @staticmethod
    def explain(reasons: list[str]) -> list[str]:
        """Map internal rejection codes to short, human-readable category
        labels for diagnostic display (e.g. main.py's --show-rejections).

        Note: Hard Filter only checks data-availability/quality gates
        (Document 02 SS15) - it does not evaluate Trend, Momentum, or
        ATR. Those are Scoring Engine indicators (Document 03) and only
        run *after* a symbol passes the Hard Filter, so they can never
        appear as a Hard Filter rejection reason.
        """
        labels: list[str] = []
        for reason in reasons:
            if reason.startswith("missing_or_insufficient_candles"):
                tf = reason.split(":", 1)[1] if ":" in reason else "?"
                labels.append(f"Candle History ({tf})")
            elif reason == "missing_open_interest":
                labels.append("Open Interest")
            elif reason == "missing_funding":
                labels.append("Funding")
            elif reason == "missing_quote_volume" or reason.startswith("quote_volume_below_threshold"):
                labels.append("Volume (Liquidity)")
            elif reason.startswith("spread_above_threshold"):
                labels.append("Spread")
            elif reason.startswith("corrupted_candle_data"):
                tf = reason.split(":", 1)[1] if ":" in reason else "?"
                labels.append(f"Corrupted Data ({tf})")
            else:
                labels.append(reason)
        return labels
