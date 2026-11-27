"""
QDE V1 - indicators/registry.py

Indicator plugin registry (Document 09 SS11 addendum, mandatory frozen
decision). ScoringEngine must never import a concrete indicator module
by name - it only calls IndicatorRegistry.get_all().

Adding a new indicator requires only:
    1. Create the new indicator module in indicators/.
    2. Decorate its class with @register_indicator.
    3. Add a weight for it in config/weights.json.
No other engine requires modification (Document 06 SS13).

Dependency level: 2 (depends on indicators.base; discover() dynamically
imports sibling indicator modules at call time to avoid import cycles
with indicators/__init__.py).
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Type

from indicators.base import BaseIndicator

# Module-level registry populated by the @register_indicator decorator
# as each indicator module is imported.
_REGISTRY: dict[str, Type[BaseIndicator]] = {}

_NON_INDICATOR_MODULES = {"base", "registry", "__init__"}


def register_indicator(cls: Type[BaseIndicator]) -> Type[BaseIndicator]:
    """Class decorator. Registers `cls` under its `name` attribute.

    Example:
        @register_indicator
        class TrendIndicator(BaseIndicator):
            name = "trend"
            family = FeatureFamily.TREND
            def calculate(self, market_data): ...
    """
    if not cls.name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'name' attribute")
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        raise ValueError(f"Duplicate indicator name registered: '{cls.name}'")
    _REGISTRY[cls.name] = cls
    return cls


class IndicatorRegistry:
    """Holds one instantiated indicator per registered class.

    ScoringEngine consumes `get_all()` and never knows the concrete
    indicator class names.
    """

    def __init__(self) -> None:
        self._instances: list[BaseIndicator] = []

    def register(self, indicator: BaseIndicator) -> None:
        self._instances.append(indicator)

    def get_all(self) -> list[BaseIndicator]:
        return list(self._instances)

    def get_by_name(self, name: str) -> BaseIndicator | None:
        for indicator in self._instances:
            if indicator.name == name:
                return indicator
        return None

    def names(self) -> list[str]:
        return [i.name for i in self._instances]

    @classmethod
    def discover(cls) -> "IndicatorRegistry":
        """Import every module in the indicators package so that every
        @register_indicator decorator runs, then build a registry with
        one instance per registered class.

        The import of the `indicators` package happens lazily here
        (not at indicators/registry.py's own import time) to avoid a
        circular import between indicators/__init__.py and this module.
        """
        import indicators as indicators_package

        for _, module_name, _ in pkgutil.iter_modules(indicators_package.__path__):
            if module_name in _NON_INDICATOR_MODULES:
                continue
            importlib.import_module(f"indicators.{module_name}")

        registry = cls()
        for indicator_cls in _REGISTRY.values():
            registry.register(indicator_cls())
        return registry
