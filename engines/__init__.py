"""
QDE V1 - engines package

Business logic layer: filtering, scoring, confidence, and research
persistence. Depends on models/, indicators/ (registry only), and
storage/ (abstractions only - never a concrete DB driver directly).
"""

from engines.hard_filter import HardFilterEngine
from engines.scoring import ScoringEngine
from engines.uniqueness import UniquenessProvider, BatchUniquenessProvider, HistoricalUniquenessProvider
from engines.confidence import ConfidenceEngine
from engines.research import ResearchEngine

__all__ = [
    "HardFilterEngine",
    "ScoringEngine",
    "UniquenessProvider",
    "BatchUniquenessProvider",
    "HistoricalUniquenessProvider",
    "ConfidenceEngine",
    "ResearchEngine",
]
