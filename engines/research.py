"""
QDE V1 - engines/research.py

Research Engine (Document 05). Collects evidence and preserves
knowledge - it does not execute trades, score setups, or calculate
indicators (Document 05 SS19).

Persistence goes exclusively through the injected SetupRepository
abstraction (storage.repository), never a concrete database driver -
the frozen Repository-pattern decision (Document 09 SS11 addendum).

V1 scope note: true MFE/MAE (Document 05 SS7-SS8) require continuous
post-signal price tracking over multiple horizons, which is a
scheduling concern outside this engine's synchronous scan pipeline
(Document 05 SS6: "revisited automatically"). This engine exposes
`record_performance` as the write path such a scheduler will call, and
`compute_simple_return` as a placeholder single-point estimator, so the
public API will not need to change once the scheduler exists.

Dependency level: 2 (depends on models.setup, models.result,
storage.repository, storage.logger).
"""

from __future__ import annotations

from typing import Any

from models.result import Result
from storage.logger import QDELogger
from storage.repository import SetupRepository


class ResearchEngine:
    """Records every Setup/Result and exposes historical statistics.

    Per Document 05 SS3: no setup is ever discarded, regardless of
    whether it passed the Hard Filter or scored well.
    """

    def __init__(self, repository: SetupRepository) -> None:
        self.repository = repository
        self.logger = QDELogger.get(__name__)

    def record(self, result: Result, price_at_scan: float | None = None, config_version: str | None = None) -> None:
        """Persist a freshly produced Result. Always called, pass or
        fail (Document 05 SS3).

        `price_at_scan` (V1 Final Audit revision - "Data Collection
        Readiness"): forwarded to the repository so every scanned
        symbol - not just Top 15 signals or --audit-capture runs - has
        a price recorded at the moment it was scored. See
        storage.repository.SetupRepository.insert_setup's docstring
        for why this matters.

        `config_version` (Deep Audit patch item #9): a deterministic
        identifier of the config/weights.json content active when this
        setup was scored - forwarded as-is, never guessed here.
        """
        self.repository.insert_setup(result, price_at_scan=price_at_scan, config_version=config_version)
        self.logger.info(
            f"Research: recorded {result.symbol} "
            f"score={result.score} confidence={result.confidence} "
            f"passed={result.setup.passed}"
        )

    def record_performance(
        self,
        setup_id: str,
        horizon: str,
        price_at_horizon: float,
        mfe: float,
        mae: float,
    ) -> None:
        """Write path for a future-performance revisit
        (Document 05 SS6). Intended to be called by a scheduler that
        tracks price action after the signal was generated.
        """
        self.repository.update_performance(setup_id, horizon, price_at_horizon, mfe, mae)
        self.logger.debug(
            f"Research: performance recorded setup_id={setup_id} horizon={horizon} "
            f"mfe={mfe} mae={mae}"
        )

    @staticmethod
    def compute_simple_return(entry_price: float, price_at_horizon: float) -> float:
        """Single-point return estimate used as a stand-in for true
        MFE/MAE until continuous price tracking is implemented
        (see module docstring).
        """
        if not entry_price:
            return 0.0
        return round((price_at_horizon - entry_price) / entry_price, 4)

    def get_statistics(
        self,
        fingerprint: dict[str, str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Aggregate statistics (Document 05 SS11), optionally scoped
        to a specific setup fingerprint family.
        """
        if fingerprint:
            matches = self.repository.get_setups_by_fingerprint(fingerprint)
            return {"count": len(matches), "matches": matches}
        return self.repository.get_statistics(filters)

    def get_recent_setups(self, limit: int = 500) -> list[dict[str, Any]]:
        """Used by uniqueness providers that graduate from batch-only to
        historical comparison (Document 09 SS11 addendum).
        """
        return self.repository.get_recent_setups(limit)

    def start_scan_run(self, run_id: str, started_at: str) -> None:
        self.repository.start_scan_run(run_id, started_at)

    def finish_scan_run(self, run_id: str, summary: dict[str, Any]) -> None:
        self.repository.finish_scan_run(run_id, summary)
