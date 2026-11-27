"""
QDE V1 - indicators package

Every indicator is an isolated plugin (Document 06 SS6). Indicators
never import each other and never talk to the exchange, cache, or
storage directly - they only transform MarketData -> Feature.

Concrete indicators self-register via @register_indicator
(indicators.registry) when their module is imported by
IndicatorRegistry.discover().
"""

from indicators.base import BaseIndicator
from indicators.registry import IndicatorRegistry, register_indicator

__all__ = ["BaseIndicator", "IndicatorRegistry", "register_indicator"]
