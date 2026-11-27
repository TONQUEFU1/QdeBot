"""
QDE V1 - tests/test_indicators.py

Unit tests for indicators/ (Document 06 SS11).
"""

from __future__ import annotations

import math

from indicators.base import BaseIndicator
from indicators.registry import IndicatorRegistry
from models.feature import Feature, FeatureFamily, FeatureStatus

EXPECTED_INDICATOR_NAMES = {
    "trend",
    "momentum",
    "volatility",
    "volume",
    "relative_strength",
    "open_interest",
    "funding",
}


def test_registry_discovers_all_indicators():
    registry = IndicatorRegistry.discover()
    assert set(registry.names()) == EXPECTED_INDICATOR_NAMES


def test_every_indicator_implements_base_indicator():
    registry = IndicatorRegistry.discover()
    for indicator in registry.get_all():
        assert isinstance(indicator, BaseIndicator)
        assert indicator.name
        assert isinstance(indicator.family, FeatureFamily)


def test_every_indicator_returns_valid_feature_on_healthy_data(healthy_market_data):
    registry = IndicatorRegistry.discover()
    for indicator in registry.get_all():
        feature = indicator.calculate(healthy_market_data)
        assert isinstance(feature, Feature)
        assert feature.name == indicator.name
        assert isinstance(feature.status, FeatureStatus)
        assert not math.isnan(feature.value)
        assert not math.isnan(feature.score)


def test_every_indicator_handles_empty_data_gracefully(empty_market_data):
    registry = IndicatorRegistry.discover()
    for indicator in registry.get_all():
        feature = indicator.calculate(empty_market_data)
        assert isinstance(feature, Feature)
        assert feature.status == FeatureStatus.NEUTRAL
        assert feature.score == 0.0
        assert feature.metadata.get("available") is False


def test_trend_indicator_detects_bullish_alignment(healthy_market_data):
    registry = IndicatorRegistry.discover()
    trend = registry.get_by_name("trend")
    feature = trend.calculate(healthy_market_data)
    assert feature.status == FeatureStatus.POSITIVE
    assert feature.metadata["bullish_alignment"] is True


def test_volume_indicator_detects_expansion(healthy_market_data):
    registry = IndicatorRegistry.discover()
    volume = registry.get_by_name("volume")
    feature = volume.calculate(healthy_market_data)
    assert feature.metadata["ratio"] > 1.0


def test_relative_strength_uses_btc_baseline_when_available(healthy_market_data):
    registry = IndicatorRegistry.discover()
    rs = registry.get_by_name("relative_strength")
    feature = rs.calculate(healthy_market_data)
    assert feature.metadata["baseline"] == "btc"


def test_register_indicator_rejects_empty_name():
    from indicators.registry import register_indicator

    class Broken(BaseIndicator):
        name = ""
        family = FeatureFamily.STRUCTURE

        def calculate(self, market_data):
            return self._unavailable("n/a")

    try:
        register_indicator(Broken)
        assert False, "expected ValueError for empty name"
    except ValueError:
        pass
