"""
QDE V1 - storage/repository.py

Abstract persistence interface (Repository pattern - Document 09 SS11
addendum, mandatory frozen decision).

SQLite is the V1 implementation (storage.sqlite_repository), but no
business-logic module (engines.research, main.py) may depend on a
concrete database driver. Everything depends on this abstraction, so
PostgreSQL/TimescaleDB can replace SQLite later without touching
engines/ or main.py.

    SetupRepository (ABC)
            ^
            |
    SQLiteRepository        (V1 - implemented)
    PostgresRepository       (V2 - future)
    TimescaleRepository       (V3 - future)

Dependency level: 1 (depends only on models.result).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from models.result import Result


class SetupRepository(ABC):
    """Abstract persistence boundary for Setup/Result records.

    Every method here corresponds directly to a need expressed in
    Document 05 (Research Engine) and Document 09 SS9 (database schema).
    Concrete implementations must honor these semantics exactly.
    """

    @abstractmethod
    def insert_setup(
        self,
        result: Result,
        price_at_scan: float | None = None,
        config_version: str | None = None,
    ) -> None:
        """Persist a newly produced Result. Must be called for every
        setup regardless of pass/fail (Document 05 SS3: no setup is
        ever discarded).

        `price_at_scan` (V1 Final Audit revision - "Data Collection
        Readiness" data-schema addition): the symbol's price at the
        moment this setup was scored. Previously this was recorded
        only for Top 15 signals (performance_tracker.py) or when
        --audit-capture was explicitly enabled (audit_engine.py) -
        meaning most scanned symbols, most days, had no price ever
        persisted, making it permanently impossible to later ask "what
        happened to the coins we scored well but didn't rank in Top
        15?" (Document 05's own False Negative analysis goal).
        Optional/backward compatible: existing callers that don't pass
        it simply record NULL, same as before this field existed.

        `config_version` (Deep Audit patch item #9): a deterministic
        identifier (hash or version string) of the config/weights.json
        content active when this setup was scored - so a future
        calibration effort can tell which setups were produced under
        which parameters. Optional/backward compatible; existing rows
        and callers that omit it get NULL, never a guessed value.
        """
        raise NotImplementedError

    @abstractmethod
    def update_performance(
        self,
        setup_id: str,
        horizon: str,
        price_at_horizon: float,
        mfe: float,
        mae: float,
    ) -> None:
        """Record a future-performance revisit for a previously stored
        setup (Document 05 SS6-SS8). `horizon` is one of
        "1h", "6h", "12h", "24h", "48h", "72h", "7d".

        NOTE (Deep Audit finding A5): this method has existed since
        V1's original design but was never called from anywhere in the
        codebase - the actual Fixed Horizon Outcome system (patch item
        P1) uses the new insert_outcome()/get_pending_outcomes() methods
        below instead, which additionally track target_timestamp,
        return_pct, high/low, completed, and data_quality - richer than
        this method's original signature. This method is kept, unused,
        for backward compatibility with the existing abstract interface;
        it is not removed per the "destructive migration yapılmamalı"
        rule.
        """
        raise NotImplementedError

    @abstractmethod
    def insert_outcome(
        self,
        setup_id: str,
        horizon: str,
        target_timestamp: str | None,
        target_price: float | None,
        return_pct: float | None,
        high: float | None,
        low: float | None,
        mfe: float | None,
        mae: float | None,
        completed: bool,
        data_quality: str,
    ) -> None:
        """Fixed Horizon Outcome (Deep Audit patch P1). Records the
        REAL, historical-OHLCV-derived outcome for one (setup_id,
        horizon) pair - never a "current price at report-generation
        time" (Deep Audit finding A3/A5; this method's caller,
        outcome_engine.py, is the only code in this project permitted
        to determine target_price, and it is contractually forbidden
        from using a live/current price for it).

        Immutability: once a row for (setup_id, horizon) exists with
        completed=True, implementations MUST NOT overwrite it on a
        subsequent call - a completed, real outcome is permanent.
        Rows with completed=False (missing historical data) MAY be
        replaced by a later call that successfully finds the data,
        since nothing real was ever recorded to protect.
        """
        raise NotImplementedError

    @abstractmethod
    def get_outcome(self, setup_id: str, horizon: str) -> dict[str, Any] | None:
        """Returns the previously recorded outcome for this
        (setup_id, horizon), or None if it has never been attempted."""
        raise NotImplementedError

    @abstractmethod
    def get_pending_outcome_targets(self, as_of_iso: str, horizons: dict[str, int]) -> list[dict[str, Any]]:
        """Returns every (setup, horizon) combination whose
        target_timestamp (setup.timestamp + horizon seconds) is at or
        before `as_of_iso`, and which does NOT already have a
        completed=True outcome recorded. `horizons` maps horizon name
        to its duration in seconds (e.g. {"30m": 1800, "1h": 3600}).

        Each returned dict includes at least: setup_id, symbol,
        scan_timestamp (the setup's own timestamp), price_at_scan
        (used as the entry price for return/MFE/MAE calculations),
        horizon, target_timestamp (computed, ISO8601).
        """
        raise NotImplementedError

    @abstractmethod
    def get_setups_by_fingerprint(self, fingerprint: dict[str, str]) -> list[dict[str, Any]]:
        """Retrieve historical setups matching a given fingerprint
        structure, used for setup-family statistics (Document 05 SS11).
        """
        raise NotImplementedError

    @abstractmethod
    def get_statistics(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Aggregate win rate / average MFE / average MAE / average
        duration / consistency score, optionally filtered by market
        regime, fingerprint fields, or date range (Document 05 SS11-SS12).
        """
        raise NotImplementedError

    @abstractmethod
    def get_recent_setups(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return the most recent stored setups. Used by uniqueness
        providers that graduate from batch-only to historical
        comparison (Document 09 SS11 addendum) without changing their
        public API.
        """
        raise NotImplementedError

    @abstractmethod
    def update_uniqueness(self, setup_id: str, uniqueness: float) -> None:
        """Overwrite a previously-recorded setup's uniqueness value.

        Needed because uniqueness is computed relative to the full
        scan batch (Document 04 SS13), but insert_setup() is called
        per-symbol, before the batch is complete (Document 06 SS8
        pipeline order). main.QDEPipeline recomputes the true
        batch-relative uniqueness once ranking has the full batch,
        then calls this to correct the already-persisted row rather
        than leaving a stale placeholder in permanent storage.
        """
        raise NotImplementedError

    @abstractmethod
    def record_regime_snapshot(
        self,
        scan_id: str,
        timestamp: str,
        regime: str,
        btc_volatility_percentile: float | None,
        btc_bullish_alignment: bool | None,
        btc_bearish_alignment: bool | None,
    ) -> None:
        """Persists the raw BTC indicator values that produced a given
        scan's market_regime label (V1 Final Audit revision - "Data
        Collection Readiness" data-schema addition).

        Every Setup already stores the final regime string
        (e.g. "bull"), but not the underlying values
        market_regime.classify_regime() used to arrive at it. Without
        this, retroactively asking "would a different volatility
        threshold have classified today differently?" during V2 tuning
        is impossible - the raw inputs would already be gone.
        """
        raise NotImplementedError

    @abstractmethod
    def start_scan_run(self, run_id: str, started_at: str) -> None:
        """Register the start of a scan run (scan_runs table,
        Document 09 SS9).
        """
        raise NotImplementedError

    @abstractmethod
    def finish_scan_run(self, run_id: str, summary: dict[str, Any]) -> None:
        """Finalize a scan run record with aggregate statistics."""
        raise NotImplementedError
