"""
QDE V1 - tests/test_confidence.py

Unit tests for engines/confidence.py and engines/uniqueness.py
(Document 04).
"""

from __future__ import annotations

import pytest

from engines.confidence import ConfidenceEngine
from engines.uniqueness import (
    BatchUniquenessProvider,
    HistoricalUniquenessProvider,
    UniquenessProvider,
)
from models.result import RISK_GRADES, Result
from models.setup import Setup


def _make_setup(fingerprint: dict[str, str], raw_score: float = 85.0) -> Setup:
    return Setup(
        symbol="ETHFIUSDT",
        timestamp="2026-07-11T00:00:00Z",
        market_regime="Bull",
        raw_score=raw_score,
        feature_scores={k: 10.0 for k in fingerprint},
        fingerprint=fingerprint,
        passed=True,
    )


def test_agreeing_features_produce_high_confidence():
    setup = _make_setup(
        {
            "trend": "positive",
            "momentum": "positive",
            "volume": "positive",
            "relative_strength": "positive",
        }
    )
    engine = ConfidenceEngine(uniqueness_provider=BatchUniquenessProvider())
    result = engine.evaluate(setup, batch_context=[])
    assert isinstance(result, Result)
    assert result.confidence > 60.0
    assert result.contradictions == []


def test_conflicting_features_within_family_flagged():
    setup = _make_setup(
        {
            "volume": "positive",
            "open_interest": "negative",  # same family (participation) conflict
            "funding": "positive",
        }
    )
    engine = ConfidenceEngine(uniqueness_provider=BatchUniquenessProvider())
    result = engine.evaluate(setup, batch_context=[])
    assert any("participation" in c for c in result.contradictions)


def test_risk_grade_is_always_valid():
    setup = _make_setup({"trend": "negative"}, raw_score=10.0)
    engine = ConfidenceEngine(uniqueness_provider=BatchUniquenessProvider())
    result = engine.evaluate(setup, batch_context=[])
    assert result.risk_grade in RISK_GRADES


def test_batch_uniqueness_default_when_no_context():
    setup = _make_setup({"trend": "positive"})
    provider = BatchUniquenessProvider()
    assert provider.calculate(setup, context=[]) == 50.0


def test_batch_uniqueness_lower_when_fingerprint_common():
    setup = _make_setup({"trend": "positive"})
    identical_setup = _make_setup({"trend": "positive"})
    different_setup = _make_setup({"trend": "negative"})
    provider = BatchUniquenessProvider()

    common_context = [identical_setup] * 20
    rare_context = [different_setup] * 20

    common_score = provider.calculate(setup, context=common_context)
    rare_score = provider.calculate(setup, context=rare_context)
    assert rare_score > common_score


def test_historical_uniqueness_provider_is_frozen_stub():
    provider = HistoricalUniquenessProvider(repository=None)
    assert isinstance(provider, UniquenessProvider)
    with pytest.raises(NotImplementedError):
        provider.calculate(_make_setup({"trend": "positive"}), context=[])
