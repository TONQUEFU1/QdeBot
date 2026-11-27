"""
QDE V1 - indicators/funding.py

Funding indicator (Document 03 SS3.H). Used as a positioning filter -
never dominates the score, only adjusts confidence/quality
(Document 03 SS3.H: "Funding should never dominate the score").

Dependency level: 3 (depends on indicators.base, indicators.registry,
models.feature, models.market_data).
"""

from __future__ import annotations

from indicators.base import BaseIndicator
from indicators.registry import register_indicator
from models.feature import Feature, FeatureFamily, FeatureStatus
from models.market_data import MarketData

_HEALTHY_THRESHOLD = 0.0001   # |rate| at or below this = healthy/neutral positioning
_CROWDED_THRESHOLD = 0.0005   # |rate| above this = overcrowded positioning


@register_indicator
class FundingIndicator(BaseIndicator):
    name = "funding"
    family = FeatureFamily.PARTICIPATION

    def calculate(self, market_data: MarketData) -> Feature:
        rate = market_data.funding.get("rate")
        if rate is None:
            return self._unavailable("funding_rate_missing")

        abs_rate = abs(rate)

        if abs_rate <= _HEALTHY_THRESHOLD:
            score, status = 8.0, FeatureStatus.POSITIVE
        elif abs_rate <= _CROWDED_THRESHOLD:
            score, status = 2.0, FeatureStatus.NEUTRAL
        else:
            # Deliberately capped low magnitude - funding is a soft
            # positioning signal, not a dominant score driver.
            score, status = -10.0, FeatureStatus.NEGATIVE

        basis_points = rate * 10_000
        return Feature(
            name=self.name,
            value=self._clamp(50.0 + basis_points, lo=0.0, hi=100.0),
            score=score,
            status=status,
            family=self.family,
            metadata={
                "rate": rate,
                "basis_points": basis_points,
                "crowded_long": rate > _CROWDED_THRESHOLD,
                "crowded_short": rate < -_CROWDED_THRESHOLD,
            },
        )
