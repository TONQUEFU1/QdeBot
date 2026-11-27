"""
QDE V1 - indicators/relative_strength.py

Relative Strength indicator (Document 03 SS3.F) - "one of the highest
priority features": does the coin outperform BTC?

V1 data-availability note:
    BaseIndicator.calculate() receives only the target symbol's
    MarketData (Document 06 SS6 plugin isolation - indicators never
    call other indicators or the exchange directly). To compare
    against BTC without violating that isolation, this indicator reads
    an optional `btc_percentage` key that data.collector may attach to
    `market_data.ticker` (the 24h % change of BTCUSDT for the same
    scan). If absent, the indicator falls back to the symbol's own 24h
    change as a lower-confidence proxy rather than crashing or
    fabricating a comparison (Document 03 SS13: missing feature ->
    reduced confidence, not crash). Wiring the BTC baseline into every
    ticker is a collector-level enhancement tracked for the next
    implementation pass and does not require any change to this
    indicator's interface.

Dependency level: 3 (depends on indicators.base, indicators.registry,
models.feature, models.market_data).
"""

from __future__ import annotations

from indicators.base import BaseIndicator
from indicators.registry import register_indicator
from models.feature import Feature, FeatureFamily, FeatureStatus
from models.market_data import MarketData


@register_indicator
class RelativeStrengthIndicator(BaseIndicator):
    name = "relative_strength"
    family = FeatureFamily.MARKET

    def calculate(self, market_data: MarketData) -> Feature:
        percentage = market_data.ticker.get("percentage")
        if percentage is None:
            return self._unavailable("ticker_percentage_missing")

        btc_percentage = market_data.ticker.get("btc_percentage")

        if btc_percentage is not None:
            rs = percentage - btc_percentage
            score = max(-15.0, min(20.0, rs))
            status = (
                FeatureStatus.POSITIVE
                if rs > 1.0
                else FeatureStatus.NEGATIVE
                if rs < -1.0
                else FeatureStatus.NEUTRAL
            )
            value = self._clamp(50.0 + rs)
            metadata = {
                "symbol_change_pct": percentage,
                "btc_change_pct": btc_percentage,
                "relative_strength_pct": rs,
                "baseline": "btc",
            }
        else:
            # Lower-confidence fallback: symbol's own momentum only.
            score = max(-5.0, min(5.0, percentage / 2.0))
            status = (
                FeatureStatus.POSITIVE
                if percentage > 0
                else FeatureStatus.NEGATIVE
                if percentage < 0
                else FeatureStatus.NEUTRAL
            )
            value = self._clamp(50.0 + percentage)
            metadata = {
                "symbol_change_pct": percentage,
                "baseline": "self_only_v1_fallback",
            }

        return Feature(
            name=self.name,
            value=value,
            score=score,
            status=status,
            family=self.family,
            metadata=metadata,
        )
