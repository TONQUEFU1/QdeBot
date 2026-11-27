"""
QDE V1 - models/setup.py

The Setup record (Document 03 SS15, Document 05 SS17).

Produced by engines.scoring.ScoringEngine, then enriched into a Result
by engines.confidence.ConfidenceEngine. Persisted as-is (via its
fingerprint/feature_scores) by engines.research.ResearchEngine.

Dependency level: 0 (no internal imports). Pure data holder only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import uuid


@dataclass
class Setup:
    """A single scored market setup for one symbol at one point in time.

    Example (Document 03 SS15):
        {
          "symbol": "ETHFIUSDT",
          "raw_score": 91,
          "feature_scores": {"trend": 18, "momentum": 15, ...},
          "penalties": [],
          "passed": true
        }
    """

    symbol: str
    timestamp: str  # UTC ISO8601
    market_regime: str
    raw_score: float
    feature_scores: dict[str, float] = field(default_factory=dict)
    penalties: list[str] = field(default_factory=list)
    fingerprint: dict[str, str] = field(default_factory=dict)
    passed: bool = False
    setup_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def total_penalty(self) -> float:
        """Soft penalties reduce score without rejecting the setup
        (Document 03 SS7). Penalty reasons are stored as strings;
        their numeric impact is already folded into raw_score by the
        Scoring Engine, so this is purely informational/explainability.
        """
        return float(len(self.penalties))

    def top_contributing_features(self, n: int = 3) -> list[tuple[str, float]]:
        """Used by the Explainable AI principle (Document 03 SS14) to
        show which categories drove the score.
        """
        return sorted(
            self.feature_scores.items(), key=lambda kv: kv[1], reverse=True
        )[:n]

    def to_dict(self) -> dict[str, Any]:
        """Serialization helper for storage.repository implementations."""
        return {
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "market_regime": self.market_regime,
            "raw_score": self.raw_score,
            "feature_scores": self.feature_scores,
            "penalties": self.penalties,
            "fingerprint": self.fingerprint,
            "passed": self.passed,
        }
