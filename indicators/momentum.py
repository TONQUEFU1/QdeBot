"""
QDE V1 - indicators/momentum.py

Momentum Quality indicator (Document 03 SS3.B). Evaluates whether
momentum is starting rather than ending, via RSI on the 15m timeframe
(Document 02 SS7: momentum uses 15m).

Dependency level: 3 (depends on indicators.base, indicators.registry,
models.feature, models.market_data).
"""

from __future__ import annotations

from indicators.base import BaseIndicator
from indicators.registry import register_indicator
from models.feature import Feature, FeatureFamily, FeatureStatus
from models.market_data import MarketData

_TIMEFRAME = "15m"
_RSI_PERIOD = 14

# Healthy RSI range: momentum is beginning, not already exhausted.
_HEALTHY_LOW, _HEALTHY_HIGH = 45.0, 65.0
_OVEREXTENDED = 75.0
_WEAK = 30.0


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@register_indicator
class MomentumIndicator(BaseIndicator):
    name = "momentum"
    family = FeatureFamily.MOMENTUM

    def calculate(self, market_data: MarketData) -> Feature:
        tf = market_data.get_timeframe(_TIMEFRAME)
        if tf is None:
            return self._unavailable("timeframe_missing")

        closes = tf.closes()
        rsi = _rsi(closes, _RSI_PERIOD)
        if rsi is None:
            return self._unavailable("insufficient_candle_history")

        if _HEALTHY_LOW <= rsi <= _HEALTHY_HIGH:
            score, status = 15.0, FeatureStatus.POSITIVE
        elif rsi >= _OVEREXTENDED:
            score, status = -12.0, FeatureStatus.NEGATIVE
        elif rsi <= _WEAK:
            score, status = -8.0, FeatureStatus.NEGATIVE
        else:
            # Transitional zone: mild positive/negative lean toward the
            # healthy band without full credit.
            distance = min(abs(rsi - _HEALTHY_LOW), abs(rsi - _HEALTHY_HIGH))
            score = max(0.0, 8.0 - distance * 0.5)
            status = FeatureStatus.NEUTRAL

        return Feature(
            name=self.name,
            value=self._clamp(rsi),
            score=score,
            status=status,
            family=self.family,
            metadata={"timeframe": tf.timeframe, "rsi": rsi},
        )
