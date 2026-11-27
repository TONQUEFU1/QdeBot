"""
QDE V1 - engines/uniqueness.py

Uniqueness scoring abstraction (Document 04 SS13-SS14, Document 09 SS11
addendum - mandatory frozen decision). Measures how rare a setup's
fingerprint/score is, not how good it is (Document 04 SS14).

ConfidenceEngine depends on the UniquenessProvider interface only, via
constructor injection, so V1's batch-only implementation can later be
replaced by a historical-database implementation without changing
ConfidenceEngine's public API:

    UniquenessProvider (ABC)
            ^
            |
    BatchUniquenessProvider        (V1 - implemented, current scan batch)
    HistoricalUniquenessProvider     (V2 - stub, will query
                                       storage.repository.SetupRepository
                                       .get_recent_setups() /
                                       get_statistics())

Dependency level: 1 (depends only on models.setup).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.setup import Setup


class UniquenessProvider(ABC):
    """Abstract uniqueness calculator. `context` is the comparison
    population against which rarity is measured - its meaning depends
    on the concrete implementation (current batch, full history, etc.).
    """

    @abstractmethod
    def calculate(self, setup: Setup, context: list[Setup]) -> float:
        """Return a uniqueness score in [0, 100]. Higher = rarer."""
        raise NotImplementedError


class BatchUniquenessProvider(UniquenessProvider):
    """V1 implementation: uniqueness relative to the current scan batch
    only (Document 09 SS11 addendum). Combines fingerprint rarity with
    raw_score percentile rank within the batch.

    Example (Document 04 SS13): out of 500 scanned coins, only 3 share
    a similar fingerprint and score profile -> those 3 receive
    uniqueness 95+.
    """

    _NEUTRAL_DEFAULT = 50.0

    def calculate(self, setup: Setup, context: list[Setup]) -> float:
        if not context:
            return self._NEUTRAL_DEFAULT

        matching = [s for s in context if s.fingerprint == setup.fingerprint]
        fingerprint_rarity = 1.0 - (len(matching) / len(context))

        scores = sorted(s.raw_score for s in context)
        rank = sum(1 for s in scores if s <= setup.raw_score)
        score_percentile = rank / len(scores)

        # Rarity of structure matters more than raw score percentile
        # for the Uniqueness concept (Document 04 SS14: "rare does not
        # always mean good").
        uniqueness = (fingerprint_rarity * 0.7 + score_percentile * 0.3) * 100.0
        return max(0.0, min(100.0, round(uniqueness, 2)))


class HistoricalUniquenessProvider(UniquenessProvider):
    """V2 stub: will compute uniqueness against the full historical
    setup database (via storage.repository.SetupRepository) instead of
    just the current batch. Not implemented in V1 - present only to
    document the frozen extension point (Document 09 SS11 addendum).
    Swapping BatchUniquenessProvider for this class in main.py requires
    no change to ConfidenceEngine's public API.
    """

    def __init__(self, repository: "SetupRepository") -> None:  # noqa: F821
        self.repository = repository

    def calculate(self, setup: Setup, context: list[Setup]) -> float:
        raise NotImplementedError(
            "HistoricalUniquenessProvider is a V2 extension point, not yet implemented."
        )
