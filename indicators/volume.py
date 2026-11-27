"""
QDE V1 - indicators/volume.py

Volume Quality indicator (Document 03 SS3.E). Measures participation
via the current candle's volume relative to its recent average
(volume expansion), on the 15m timeframe.

Dependency level: 3 (depends on indicators.base, indicators.registry,
models.feature, models.market_data).
"""

from __future__ import annotations

from indicators.base import BaseIndicator
from indicators.registry import register_indicator
from models.feature import Feature, FeatureFamily, FeatureStatus
from models.market_data import MarketData

_TIMEFRAME = "15m"
_LOOKBACK = 20  # candles used to compute the baseline average

_STRONG_EXPANSION = 2.0
_MILD_EXPANSION = 1.3
_WEAK_RATIO = 0.6


@register_indicator
class VolumeIndicator(BaseIndicator):
    name = "volume"
    family = FeatureFamily.PARTICIPATION

    def calculate(self, market_data: MarketData) -> Feature:
        tf = market_data.get_timeframe(_TIMEFRAME)
        if tf is None:
            return self._unavailable("timeframe_missing")

        volumes = tf.volumes()
        if len(volumes) < _LOOKBACK + 1:
            return self._unavailable("insufficient_candle_history")

        current = volumes[-1]
        baseline_window = volumes[-(_LOOKBACK + 1) : -1]
        average = sum(baseline_window) / len(baseline_window)
        if average <= 0:
            return self._unavailable("zero_baseline_volume")

        ratio = current / average

        if ratio >= _STRONG_EXPANSION:
            score, status = 18.0, FeatureStatus.POSITIVE
        elif ratio >= _MILD_EXPANSION:
            score, status = 10.0, FeatureStatus.POSITIVE
        elif ratio <= _WEAK_RATIO:
            score, status = -10.0, FeatureStatus.NEGATIVE
        else:
            score = max(-3.0, min(3.0, (ratio - 1.0) * 10.0))
            status = FeatureStatus.NEUTRAL

        return Feature(
            name=self.name,
            value=self._clamp(ratio * 50.0),
            score=score,
            status=status,
            family=self.family,
            metadata={"timeframe": tf.timeframe, "ratio": ratio, "average": average},
        )
