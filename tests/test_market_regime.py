"""
QDE V1.1 - tests/test_market_regime.py

Regression tests for market_regime.py's classify_regime() - the fix
for market_regime always being "unknown" (Document 10 SS2.2).
"""

from __future__ import annotations

from models.feature import Feature, FeatureFamily, FeatureStatus
from market_regime import (
    classify_regime,
    REGIME_BULL,
    REGIME_BEAR,
    REGIME_SIDEWAYS,
    REGIME_HIGH_VOLATILITY,
    REGIME_UNKNOWN,
)


def _trend_feature(bullish: bool | None = None, bearish: bool | None = None) -> Feature:
    return Feature(
        name="trend", value=50.0, score=0.0, status=FeatureStatus.NEUTRAL, family=FeatureFamily.TREND,
        metadata={"bullish_alignment": bullish, "bearish_alignment": bearish},
    )


def _volatility_feature(percentile: float | None) -> Feature:
    return Feature(
        name="volatility", value=50.0, score=0.0, status=FeatureStatus.NEUTRAL, family=FeatureFamily.STRUCTURE,
        metadata={"percentile": percentile},
    )


def test_bullish_trend_classified_as_bull():
    features = [_trend_feature(bullish=True, bearish=False), _volatility_feature(30.0)]
    assert classify_regime(features) == REGIME_BULL


def test_bearish_trend_classified_as_bear():
    features = [_trend_feature(bullish=False, bearish=True), _volatility_feature(30.0)]
    assert classify_regime(features) == REGIME_BEAR


def test_no_clear_trend_classified_as_sideways():
    features = [_trend_feature(bullish=False, bearish=False), _volatility_feature(30.0)]
    assert classify_regime(features) == REGIME_SIDEWAYS


def test_high_volatility_takes_precedence_over_trend():
    """Document 04 SS12: high volatility affects expected move/risk
    regardless of direction - checked first, before trend direction.
    """
    features = [_trend_feature(bullish=True, bearish=False), _volatility_feature(85.0)]
    assert classify_regime(features) == REGIME_HIGH_VOLATILITY


def test_high_volatility_threshold_boundary():
    features_below = [_trend_feature(bullish=True), _volatility_feature(79.9)]
    features_at = [_trend_feature(bullish=True), _volatility_feature(80.0)]
    assert classify_regime(features_below) == REGIME_BULL
    assert classify_regime(features_at) == REGIME_HIGH_VOLATILITY


def test_missing_trend_feature_returns_unknown():
    features = [_volatility_feature(30.0)]  # no trend feature at all
    assert classify_regime(features) == REGIME_UNKNOWN


def test_missing_volatility_feature_falls_back_to_trend():
    features = [_trend_feature(bullish=True, bearish=False)]  # no volatility feature
    assert classify_regime(features) == REGIME_BULL


def test_unavailable_trend_feature_returns_unknown():
    """When BTC's own trend indicator couldn't compute (insufficient
    candle history), classify_regime must not guess - it should report
    unknown, matching indicators/base.py's own "available: False"
    convention for missing data (Document 03 SS13).
    """
    unavailable_trend = Feature(
        name="trend", value=0.0, score=0.0, status=FeatureStatus.NEUTRAL, family=FeatureFamily.TREND,
        metadata={"available": False, "reason": "insufficient_candle_history"},
    )
    features = [unavailable_trend, _volatility_feature(30.0)]
    assert classify_regime(features) == REGIME_UNKNOWN


def test_empty_features_returns_unknown():
    assert classify_regime([]) == REGIME_UNKNOWN
