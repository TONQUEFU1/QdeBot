"""
QDE V1 - models/result.py

The final output object (Document 04 SS17). Wraps a Setup with the
Confidence Engine's evaluation: confidence, risk grade, uniqueness,
expected move/duration, and detected contradictions.

This is the object every output renderer (output.terminal,
output.json_output, output.csv_output) and the Research Engine consume.

Dependency level: 1 (depends only on models.setup).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.setup import Setup


@dataclass
class ExpectedMove:
    """Probability estimates for reaching given expansion thresholds
    (Document 04 SS8). Values are probabilities in [0, 1].
    """

    probabilities: dict[str, float] = field(default_factory=dict)
    # e.g. {"10_percent": 0.84, "20_percent": 0.61, "30_percent": 0.33}

    def to_dict(self) -> dict[str, float]:
        return dict(self.probabilities)


RISK_GRADES = ("A+", "A", "B", "C", "D")


@dataclass
class Result:
    """Final ranked object for one symbol (Document 04 SS17).

    Example:
        {
          "symbol": "ETHFIUSDT",
          "score": 91,
          "confidence": 94,
          "risk_grade": "A",
          "uniqueness": 96,
          "expected_move": {"10_percent": 0.84, ...},
          "expected_duration": "8-24h",
          "market_regime": "Bull",
          "contradictions": [],
          "fingerprint": {...}
        }
    """

    setup: Setup
    confidence: float
    risk_grade: str
    uniqueness: float
    expected_move: ExpectedMove
    expected_duration: str
    contradictions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.risk_grade not in RISK_GRADES:
            raise ValueError(
                f"Invalid risk_grade '{self.risk_grade}', must be one of {RISK_GRADES}"
            )

    @property
    def symbol(self) -> str:
        return self.setup.symbol

    @property
    def score(self) -> float:
        return self.setup.raw_score

    @property
    def fingerprint(self) -> dict[str, str]:
        return self.setup.fingerprint

    def has_contradictions(self) -> bool:
        return len(self.contradictions) > 0

    def is_institutional_grade(self) -> bool:
        """Signal category helper for output.terminal (Document 07 SS4)."""
        return self.risk_grade == "A+" and self.confidence >= 90

    def to_dict(self) -> dict[str, Any]:
        """Serialization helper used by storage.exporter and output.*."""
        return {
            "symbol": self.symbol,
            "score": self.score,
            "confidence": self.confidence,
            "risk_grade": self.risk_grade,
            "uniqueness": self.uniqueness,
            "expected_move": self.expected_move.to_dict(),
            "expected_duration": self.expected_duration,
            "market_regime": self.setup.market_regime,
            "contradictions": self.contradictions,
            "fingerprint": self.fingerprint,
            "feature_scores": self.setup.feature_scores,
            "passed": self.setup.passed,
            "setup_id": self.setup.setup_id,
            "timestamp": self.setup.timestamp,
        }
