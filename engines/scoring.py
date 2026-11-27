"""
QDE V1 - engines/scoring.py

Scoring Engine (Document 03). Aggregates weighted Feature scores into
a Setup with a 0-100 raw_score. Never generates buy/sell signals -
only evaluates setup quality (Document 03 SS1, SS17).

Indicator plugins are consumed via IndicatorRegistry - this engine
never imports a concrete indicator module by name (Document 09 SS11
addendum, frozen decision).

V1 simplification note: `market_regime` is not yet derived from a
dedicated regime-detection feature (Document 03 SS3.J describes this
as its own category); it is accepted as an optional parameter,
defaulting to "unknown", pending a future MarketRegimeDetector. This
does not require any change to ScoringEngine's public signature later.

Dependency level: 3 (depends on indicators.registry, models.feature,
models.setup, storage.logger).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from indicators.registry import IndicatorRegistry
from models.feature import Feature
from models.setup import Setup
from storage.logger import QDELogger


class ScoringEngine:
    """Turns a list of Feature objects into a scored Setup.

    Weights come from config/weights.json (Document 09 SS8); any
    feature without an explicit weight defaults to 1.0 so new
    indicators work immediately without a config change
    (Document 06 SS13: extensibility).
    """

    _SCORE_FLOOR = 0.0
    _SCORE_CEIL = 100.0
    _BASELINE = 50.0  # neutral starting point before feature contributions

    def __init__(self, registry: IndicatorRegistry, weights: dict[str, float]) -> None:
        self.registry = registry
        self.weights = weights
        self.logger = QDELogger.get(__name__)

    def score(self, features: list[Feature], market_regime: str = "unknown") -> Setup:
        """Aggregate `features` (typically produced by every registered
        indicator running against one symbol's MarketData) into a Setup.

        `symbol` and `passed` are intentionally left for the caller to
        set afterward (main.QDEPipeline._process_one), since scoring
        operates purely on Feature values with no knowledge of the
        symbol string or the Hard Filter's verdict.
        """
        feature_scores: dict[str, float] = {}
        penalties: list[str] = []
        fingerprint: dict[str, str] = {}
        weighted_total = 0.0

        for feature in features:
            weight = self.weights.get(feature.name, 1.0)
            weighted = feature.score * weight
            feature_scores[feature.name] = round(weighted, 2)
            weighted_total += weighted
            fingerprint[feature.name] = feature.status.value

            if feature.score < 0:
                penalties.append(f"{feature.name}:{round(feature.score, 2)}")

        raw_score = max(
            self._SCORE_FLOOR, min(self._SCORE_CEIL, self._BASELINE + weighted_total)
        )

        setup = Setup(
            symbol="",
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_regime=market_regime,
            raw_score=round(raw_score, 2),
            feature_scores=feature_scores,
            penalties=penalties,
            fingerprint=fingerprint,
            passed=False,
        )
        self.logger.debug(
            f"Score calculation: raw_score={setup.raw_score} penalties={len(penalties)}"
        )
        return setup

    def explain(self, setup: Setup) -> dict[str, Any]:
        """Explainability helper (Document 03 SS14): break down which
        categories contributed positively/negatively to the score.
        """
        return {
            "total_score": setup.raw_score,
            "breakdown": dict(setup.feature_scores),
            "penalties": list(setup.penalties),
            "top_contributors": setup.top_contributing_features(),
        }
