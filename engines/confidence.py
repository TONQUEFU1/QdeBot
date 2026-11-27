"""
QDE V1 - engines/confidence.py

Confidence Engine (Document 04). Answers "how much should we trust
this score?" - not "how good is this setup?" (Document 04 SS1).

Family-agreement design note:
    Setup.feature_scores / Setup.fingerprint are keyed by feature name
    only (Document 09 SS9 schema: feature_scores_json is a flat dict) -
    Setup does not carry each Feature's FeatureFamily. Rather than
    widen the Setup schema, this engine keeps a small, explicit
    name -> FeatureFamily map mirroring each indicator's own `family`
    class attribute (Document 04 SS4 family list). This keeps Setup
    lean and storage-friendly while still enabling the family-agreement
    matrix described in Document 04 SS4-SS6.

Uniqueness is delegated to an injected UniquenessProvider
(engines.uniqueness) - the frozen Document 09 SS11 decision.

Expected-move / expected-duration heuristics are V1 placeholders
(Document 04 SS8, SS10 both state these should ultimately come from
backtest statistics, which the Research Engine has not yet
accumulated). They will be replaced by ResearchEngine.get_statistics()
lookups in a later pass without changing this engine's public API.

Dependency level: 2 (depends on models.setup, models.result,
models.feature, engines.uniqueness, storage.logger).
"""

from __future__ import annotations

from collections import defaultdict

from engines.uniqueness import UniquenessProvider
from models.feature import FeatureFamily
from models.result import ExpectedMove, RISK_GRADES, Result
from models.setup import Setup
from storage.logger import QDELogger
from market_regime import REGIME_BULL, REGIME_BEAR, REGIME_HIGH_VOLATILITY

# Mirrors each indicator's `family` attribute (indicators/*.py).
_FAMILY_MAP: dict[str, FeatureFamily] = {
    "trend": FeatureFamily.TREND,
    "momentum": FeatureFamily.MOMENTUM,
    "volatility": FeatureFamily.STRUCTURE,
    "volume": FeatureFamily.PARTICIPATION,
    "relative_strength": FeatureFamily.MARKET,
    "open_interest": FeatureFamily.PARTICIPATION,
    "funding": FeatureFamily.PARTICIPATION,
}

# Document 04 SS12 "Market Regime Adjustment" - applied as a small,
# additive percentage-point shift to the agreement-based confidence,
# using setup.market_regime (populated by main.QDEPipeline from
# market_regime.classify_regime(), computed once per scan from BTC's
# own indicators - see market_regime.py). Values match Document 04
# SS12's own worked example (Bull +5%, Bear -15%) rather than being
# separately tuned/backtested - an honest, documented starting point,
# not a claim of optimization.
_REGIME_CONFIDENCE_ADJUSTMENT = {
    REGIME_BULL: 5.0,
    REGIME_BEAR: -15.0,
    REGIME_HIGH_VOLATILITY: 0.0,  # affects expected_move instead, see _expected_move
}


class ConfidenceEngine:
    """Evaluates a Setup's reliability and produces the final Result."""

    def __init__(self, uniqueness_provider: UniquenessProvider) -> None:
        self.uniqueness_provider = uniqueness_provider
        self.logger = QDELogger.get(__name__)

    def evaluate(self, setup: Setup, batch_context: list[Setup]) -> Result:
        family_groups: dict[str, list[str]] = defaultdict(list)
        for feature_name, status in setup.fingerprint.items():
            family = _FAMILY_MAP.get(feature_name, FeatureFamily.STRUCTURE).value
            family_groups[family].append(status)

        contradictions: list[str] = []
        agreeing_families = 0
        for family, statuses in family_groups.items():
            has_positive = "positive" in statuses
            has_negative = "negative" in statuses
            if has_positive and has_negative:
                contradictions.append(f"conflicting_signals_within_{family}")
            else:
                agreeing_families += 1

        total_families = len(family_groups) or 1
        agreement_ratio = agreeing_families / total_families
        base_confidence = agreement_ratio * 100.0 - len(contradictions) * 10.0
        # Blend with raw_score so confidence is not fully independent of
        # setup quality, while agreement remains the dominant driver.
        confidence = max(0.0, min(100.0, base_confidence)) * 0.8 + setup.raw_score * 0.2

        # Document 04 SS12 regime adjustment (see module-level
        # _REGIME_CONFIDENCE_ADJUSTMENT). Applied after the base
        # calculation, then re-clamped to [0, 100].
        regime_shift = _REGIME_CONFIDENCE_ADJUSTMENT.get(setup.market_regime, 0.0)
        confidence = round(max(0.0, min(100.0, confidence + regime_shift)), 2)

        uniqueness = self.uniqueness_provider.calculate(setup, batch_context)
        risk_grade = self._risk_grade(setup.raw_score, confidence)
        expected_move = self._expected_move(setup.raw_score, confidence, setup.market_regime)
        expected_duration = self._expected_duration(confidence)

        result = Result(
            setup=setup,
            confidence=confidence,
            risk_grade=risk_grade,
            uniqueness=uniqueness,
            expected_move=expected_move,
            expected_duration=expected_duration,
            contradictions=contradictions,
        )
        self.logger.debug(
            f"Confidence calculation: {setup.symbol or '(unassigned)'} "
            f"confidence={confidence} risk_grade={risk_grade} "
            f"contradictions={contradictions}"
        )
        return result

    @staticmethod
    def _risk_grade(raw_score: float, confidence: float) -> str:
        combined = (raw_score + confidence) / 2
        if combined >= 90:
            return RISK_GRADES[0]  # "A+"
        if combined >= 80:
            return RISK_GRADES[1]  # "A"
        if combined >= 65:
            return RISK_GRADES[2]  # "B"
        if combined >= 50:
            return RISK_GRADES[3]  # "C"
        return RISK_GRADES[4]  # "D"

    @staticmethod
    def _expected_move(raw_score: float, confidence: float, market_regime: str) -> ExpectedMove:
        """V1 heuristic placeholder (see module docstring). Document 04
        SS12: "High Volatility: Expected Move increases, Expected Risk
        also increases" - modeled here as a multiplicative bump on the
        probabilities, since higher realized volatility mechanically
        makes any given percentage move more likely to occur (whether
        favorable or adverse) within the same time window.
        """
        factor = (raw_score / 100.0) * (confidence / 100.0)
        regime_multiplier = 1.15 if market_regime == REGIME_HIGH_VOLATILITY else 1.0
        probabilities = {
            "10_percent": round(min(1.0, (0.50 + factor * 0.50) * regime_multiplier), 2),
            "20_percent": round(min(1.0, (0.30 + factor * 0.40) * regime_multiplier), 2),
            "30_percent": round(min(1.0, (0.15 + factor * 0.30) * regime_multiplier), 2),
        }
        return ExpectedMove(probabilities)

    @staticmethod
    def _expected_duration(confidence: float) -> str:
        """V1 heuristic placeholder (see module docstring)."""
        if confidence >= 85:
            return "4-12h"
        if confidence >= 65:
            return "8-24h"
        return "24-72h"
