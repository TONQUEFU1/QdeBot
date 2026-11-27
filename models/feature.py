"""
QDE V1 - models/feature.py

Per-indicator output object (Document 03, Document 06 SS6 plugin contract).

Every indicator plugin returns exactly one Feature. The Confidence
Engine groups Features by `family` to compute feature agreement
(Document 04 SS4).

Dependency level: 0 (no internal imports). Pure data holder only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FeatureStatus(str, Enum):
    """Directional read of a feature, independent of its numeric score."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class FeatureFamily(str, Enum):
    """Feature families used by the Confidence Engine's agreement matrix
    (Document 04 SS4). Agreement across families carries more weight than
    agreement within a single family.
    """

    TREND = "trend"
    MOMENTUM = "momentum"
    PARTICIPATION = "participation"  # volume, open interest, funding
    STRUCTURE = "structure"          # volatility / compression
    MARKET = "market"                # BTC trend, regime, relative strength


@dataclass(frozen=True)
class Feature:
    """Structured output of a single indicator calculation.

    Example (Document 06 SS6):
        Feature(
            name="relative_strength",
            value=84.0,
            score=15.0,
            status=FeatureStatus.POSITIVE,
            family=FeatureFamily.MARKET,
        )
    """

    name: str
    value: float
    score: float
    status: FeatureStatus
    family: FeatureFamily
    metadata: dict = field(default_factory=dict)

    def is_positive(self) -> bool:
        return self.status is FeatureStatus.POSITIVE

    def is_negative(self) -> bool:
        return self.status is FeatureStatus.NEGATIVE

    def is_available(self) -> bool:
        """Missing/unavailable features should be represented explicitly
        rather than omitted, so the Confidence Engine can reduce
        confidence instead of crashing (Document 03 SS13).
        """
        return self.status is not None and self.value is not None
