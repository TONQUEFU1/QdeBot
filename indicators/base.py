"""
QDE V1 - indicators/base.py

Plugin contract for every indicator (Document 06 SS6).

An indicator:
    - Receives the unified MarketData object.
    - Calculates exactly one feature.
    - Returns a structured Feature.
    - Never modifies global state, never calls the exchange or cache
      directly, never talks to another indicator.

Dependency level: 1 (depends on models.market_data, models.feature).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.feature import Feature, FeatureFamily
from models.market_data import MarketData


class BaseIndicator(ABC):
    """Base class every concrete indicator must subclass.

    Class attributes `name` and `family` must be overridden - they are
    used by the Scoring Engine (weights lookup) and the Confidence
    Engine (family-agreement matrix, Document 04 SS4) respectively.
    """

    name: str = ""
    family: FeatureFamily = FeatureFamily.STRUCTURE

    @abstractmethod
    def calculate(self, market_data: MarketData) -> Feature:
        """Compute this indicator's Feature for the given symbol.

        Must never raise for merely missing/insufficient data - return
        a neutral/unavailable Feature instead (Document 03 SS13:
        missing feature -> ignored, confidence reduced, not crash).
        """
        raise NotImplementedError

    @staticmethod
    def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
        """Shared normalization helper (Document 03 SS12)."""
        return max(lo, min(hi, value))

    def _unavailable(self, reason: str) -> Feature:
        """Standard 'missing data' Feature every subclass can return
        instead of crashing.
        """
        from models.feature import FeatureStatus

        return Feature(
            name=self.name,
            value=0.0,
            score=0.0,
            status=FeatureStatus.NEUTRAL,
            family=self.family,
            metadata={"reason": reason, "available": False},
        )
