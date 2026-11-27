"""
QDE V1 - indicators/open_interest.py

Open Interest indicator (Document 03 SS3.G). Measures futures
participation.

V1 data-availability note:
    Document 03 SS3.G describes OI trend states (increasing / stable /
    explosive growth / collapse), which require a historical OI series.
    MarketData carries only the current OI snapshot (Document 02 SS4)
    - historical OI tracking is a Research Engine / database concern
    (Document 05 SS4, SS17) that indicators intentionally cannot query
    (indicators must not depend on storage - Document 06 SS3 dependency
    rules). For V1, this indicator instead computes OI notional value
    (open interest amount x mark price) relative to 24h quote volume,
    a legitimate single-snapshot participation-depth proxy. True
    OI-trend scoring (increasing/collapsing) is a tracked V2 item once
    OI history is persisted per symbol.

Dependency level: 3 (depends on indicators.base, indicators.registry,
models.feature, models.market_data).
"""

from __future__ import annotations

from indicators.base import BaseIndicator
from indicators.registry import register_indicator
from models.feature import Feature, FeatureFamily, FeatureStatus
from models.market_data import MarketData

_HIGH_INTENSITY = 0.15   # OI notional / 24h quote volume
_LOW_INTENSITY = 0.02


@register_indicator
class OpenInterestIndicator(BaseIndicator):
    name = "open_interest"
    family = FeatureFamily.PARTICIPATION

    def calculate(self, market_data: MarketData) -> Feature:
        oi_amount = market_data.open_interest.get("value")
        quote_volume = market_data.quote_volume()
        mark_price = market_data.mark_price.get("mark_price") or market_data.ticker.get("last")

        if oi_amount is None or not quote_volume or not mark_price:
            return self._unavailable("oi_or_volume_or_price_missing")

        oi_notional = oi_amount * mark_price
        intensity = oi_notional / quote_volume

        if intensity >= _HIGH_INTENSITY:
            score, status = 12.0, FeatureStatus.POSITIVE
        elif intensity <= _LOW_INTENSITY:
            score, status = -6.0, FeatureStatus.NEGATIVE
        else:
            score = self._clamp(
                (intensity - _LOW_INTENSITY) / (_HIGH_INTENSITY - _LOW_INTENSITY) * 12.0,
                lo=-6.0,
                hi=12.0,
            )
            status = FeatureStatus.NEUTRAL

        return Feature(
            name=self.name,
            value=self._clamp(intensity * 500.0),  # scaled into a 0-100-ish range
            score=score,
            status=status,
            family=self.family,
            metadata={
                "oi_amount": oi_amount,
                "oi_notional": oi_notional,
                "quote_volume": quote_volume,
                "intensity": intensity,
                "reason": "snapshot_only_v1_no_history_yet",
            },
        )
