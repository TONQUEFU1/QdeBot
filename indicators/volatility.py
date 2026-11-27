"""
QDE V1 - indicators/volatility.py

Volatility Compression indicator (Document 03 SS3.C). Detects stored
energy via Bollinger Band width percentile rank on the 1h timeframe.
Longer, cleaner compressions (low percentile = tight bands relative to
recent history) receive higher scores (Document 03 SS3.C).

Dependency level: 3 (depends on indicators.base, indicators.registry,
models.feature, models.market_data).
"""

from __future__ import annotations

from indicators.base import BaseIndicator
from indicators.registry import register_indicator
from models.feature import Feature, FeatureFamily, FeatureStatus
from models.market_data import MarketData

_TIMEFRAME = "1h"
_BB_PERIOD = 20
_MIN_WIDTH_SAMPLES = 30  # minimum rolling window count for a meaningful percentile

_TIGHT_PERCENTILE = 40.0   # below this => genuine compression
_LOOSE_PERCENTILE = 80.0   # above this => expanded / no compression


def _rolling_band_widths(closes: list[float], period: int) -> list[float]:
    """Bollinger Band width = (upper - lower) / middle for every window
    of `period` closes, computed across the full closes series.
    """
    widths = []
    for i in range(period, len(closes) + 1):
        window = closes[i - period : i]
        sma = sum(window) / period
        variance = sum((x - sma) ** 2 for x in window) / period
        std = variance ** 0.5
        upper = sma + 2 * std
        lower = sma - 2 * std
        width = (upper - lower) / sma if sma else 0.0
        widths.append(width)
    return widths


def _percentile_rank(value: float, population: list[float]) -> float:
    """Percentile rank of `value` within `population`, 0-100."""
    if len(population) <= 1:
        return 50.0
    sorted_pop = sorted(population)
    below = sum(1 for v in sorted_pop if v < value)
    return (below / (len(sorted_pop) - 1)) * 100.0 if len(sorted_pop) > 1 else 50.0


@register_indicator
class VolatilityIndicator(BaseIndicator):
    name = "volatility"
    family = FeatureFamily.STRUCTURE

    def calculate(self, market_data: MarketData) -> Feature:
        tf = market_data.get_timeframe(_TIMEFRAME)
        if tf is None:
            return self._unavailable("timeframe_missing")

        closes = tf.closes()
        widths = _rolling_band_widths(closes, _BB_PERIOD)
        if len(widths) < _MIN_WIDTH_SAMPLES:
            return self._unavailable("insufficient_candle_history")

        current_width = widths[-1]
        percentile = _percentile_rank(current_width, widths)

        if percentile <= _TIGHT_PERCENTILE:
            # Tighter compression than most of recent history -> reward,
            # scaled so the tightest readings score highest.
            score = ((_TIGHT_PERCENTILE - percentile) / _TIGHT_PERCENTILE) * 15.0
            status = FeatureStatus.POSITIVE
        elif percentile >= _LOOSE_PERCENTILE:
            score = -((percentile - _LOOSE_PERCENTILE) / (100 - _LOOSE_PERCENTILE)) * 8.0
            status = FeatureStatus.NEGATIVE
        else:
            score = 0.0
            status = FeatureStatus.NEUTRAL

        return Feature(
            name=self.name,
            value=self._clamp(percentile),
            score=score,
            status=status,
            family=self.family,
            metadata={
                "timeframe": tf.timeframe,
                "band_width": current_width,
                "percentile": percentile,
            },
        )
