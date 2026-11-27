"""
QDE V1 - tests/test_scoring.py

Unit tests for engines/scoring.py (Document 03).
"""

from __future__ import annotations

from engines.scoring import ScoringEngine
from indicators.registry import IndicatorRegistry
from models.feature import Feature, FeatureFamily, FeatureStatus
from models.setup import Setup

_WEIGHTS = {
    "trend": 1.0,
    "momentum": 1.0,
    "volatility": 0.8,
    "volume": 1.0,
    "relative_strength": 1.3,
    "open_interest": 0.9,
    "funding": 0.5,
}


def test_score_produces_setup_within_bounds(healthy_market_data):
    registry = IndicatorRegistry.discover()
    engine = ScoringEngine(registry=registry, weights=_WEIGHTS)
    features = [ind.calculate(healthy_market_data) for ind in registry.get_all()]
    setup = engine.score(features, market_regime="Bull")

    assert isinstance(setup, Setup)
    assert 0.0 <= setup.raw_score <= 100.0
    assert set(setup.feature_scores.keys()) == set(registry.names())
    assert set(setup.fingerprint.keys()) == set(registry.names())
    assert setup.market_regime == "Bull"


def test_score_defaults_missing_weight_to_one():
    registry = IndicatorRegistry.discover()
    engine = ScoringEngine(registry=registry, weights={})  # no weights configured
    feature = Feature(
        name="trend", value=90.0, score=20.0, status=FeatureStatus.POSITIVE, family=FeatureFamily.TREND
    )
    setup = engine.score([feature])
    assert setup.feature_scores["trend"] == 20.0  # weight defaulted to 1.0


def test_negative_score_features_become_penalties():
    registry = IndicatorRegistry.discover()
    engine = ScoringEngine(registry=registry, weights=_WEIGHTS)
    negative_feature = Feature(
        name="momentum", value=80.0, score=-12.0, status=FeatureStatus.NEGATIVE, family=FeatureFamily.MOMENTUM
    )
    setup = engine.score([negative_feature])
    assert len(setup.penalties) == 1
    assert "momentum" in setup.penalties[0]


def test_explain_matches_setup_score():
    registry = IndicatorRegistry.discover()
    engine = ScoringEngine(registry=registry, weights=_WEIGHTS)
    feature = Feature(
        name="volume", value=70.0, score=10.0, status=FeatureStatus.POSITIVE, family=FeatureFamily.PARTICIPATION
    )
    setup = engine.score([feature])
    explanation = engine.explain(setup)
    assert explanation["total_score"] == setup.raw_score
    assert explanation["breakdown"] == setup.feature_scores
