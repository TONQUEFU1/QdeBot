"""
QDE V1 - indicators/trend.py

Trend Quality indicator (Document 03 SS3.A). Evaluates higher-timeframe
EMA alignment (Document 02 SS7: trend uses 4H).

Dependency level: 3 (depends on indicators.base, indicators.registry,
models.feature, models.market_data).
"""

from __future__ import annotations

from indicators.base import BaseIndicator
from indicators.registry import register_indicator
from models.feature import Feature, FeatureFamily, FeatureStatus
from models.market_data import MarketData

_PRIMARY_TIMEFRAME = "4h"
_FALLBACK_TIMEFRAME = "1h"
_MIN_CANDLES = 200


def _ema(values: list[float], period: int) -> list[float]:
    """Standard exponential moving average, seeded with the first value."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


@register_indicator
class TrendIndicator(BaseIndicator):
    name = "trend"
    family = FeatureFamily.TREND

    def calculate(self, market_data: MarketData) -> Feature:
        tf = market_data.get_timeframe(_PRIMARY_TIMEFRAME) or market_data.get_timeframe(
            _FALLBACK_TIMEFRAME
        )
        if tf is None or len(tf.candles) < _MIN_CANDLES:
            return self._unavailable("insufficient_candle_history")

        closes = tf.closes()
        ema20 = _ema(closes, 20)[-1]
        ema50 = _ema(closes, 50)[-1]
        ema200 = _ema(closes, 200)[-1]
        price = closes[-1]

        bullish_alignment = price > ema20 > ema50 > ema200
        bearish_alignment = price < ema20 < ema50 < ema200

        if bullish_alignment:
            score, status, value = 20.0, FeatureStatus.POSITIVE, 95.0
        elif bearish_alignment:
            score, status, value = -15.0, FeatureStatus.NEGATIVE, 5.0
        else:
            pct_from_ema200 = ((price - ema200) / ema200) * 100 if ema200 else 0.0
            score = max(-10.0, min(10.0, pct_from_ema200))
            status = (
                FeatureStatus.POSITIVE
                if pct_from_ema200 > 0
                else FeatureStatus.NEGATIVE
                if pct_from_ema200 < 0
                else FeatureStatus.NEUTRAL
            )
            value = self._clamp(50.0 + pct_from_ema200)

        return Feature(
            name=self.name,
            value=value,
            score=score,
            status=status,
            family=self.family,
            metadata={
                "timeframe": tf.timeframe,
                "ema20": ema20,
                "ema50": ema50,
                "ema200": ema200,
                "price": price,
                "bullish_alignment": bullish_alignment,
                "bearish_alignment": bearish_alignment,
            },
        )
