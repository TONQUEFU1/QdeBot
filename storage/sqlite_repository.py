"""
QDE V1 - storage/sqlite_repository.py

V1 concrete implementation of storage.repository.SetupRepository, backed
by SQLite (Document 09 SS9 schema; Document 09 SS11 addendum - kept
behind the abstract interface so it can be swapped for
PostgresRepository / TimescaleRepository later without touching
engines/ or main.py).

V1.1 thread-safety note (Async/Resource Hardening revision): main.py
now wraps every call into this class with asyncio.to_thread() so
SQLite writes don't block the event loop (Document 10 SS4: "Blocking
SQLite calls inside async code"). Python's sqlite3.Connection objects
are tied to the thread that created them by default - since this
class holds one long-lived `self._conn` across many different
asyncio.to_thread() calls (which can each land on a different worker
thread from Python's default thread pool), the connection is opened
with `check_same_thread=False`, and every method that touches
`self._conn` is additionally serialized with `self._lock`
(threading.Lock, not asyncio.Lock - the contention is between OS
threads, not asyncio tasks). `check_same_thread=False` alone would
only silence Python's same-thread assertion; it does not make
concurrent access from multiple threads safe by itself, so the lock is
required, not optional.

Deep Audit patch note: `performance_tracking` is extended additively
with the real Fixed Horizon Outcome columns (target_timestamp,
return_pct, high, low, completed, data_quality, created_at) plus a
UNIQUE index on (setup_id, horizon) enforcing the "immutable, one
outcome per setup+horizon" rule. insert_outcome() never overwrites an
existing completed=1 row - see its docstring.

Dependency level: 2 (depends on storage.repository, models.result,
storage.logger).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.result import Result
from storage.repository import SetupRepository
from storage.logger import QDELogger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS setups (
    setup_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    market_regime TEXT,
    raw_score REAL,
    confidence REAL,
    uniqueness REAL,
    risk_grade TEXT,
    fingerprint_json TEXT,
    feature_scores_json TEXT,
    passed INTEGER,
    classification TEXT
);

CREATE TABLE IF NOT EXISTS performance_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    price_at_horizon REAL,
    mfe REAL,
    mae REAL,
    checked_at TEXT NOT NULL,
    FOREIGN KEY (setup_id) REFERENCES setups (setup_id)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    symbols_scanned INTEGER,
    symbols_passed INTEGER,
    avg_confidence REAL,
    avg_score REAL
);

CREATE TABLE IF NOT EXISTS regime_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    regime TEXT,
    btc_volatility_percentile REAL,
    btc_bullish_alignment INTEGER,
    btc_bearish_alignment INTEGER
);

CREATE INDEX IF NOT EXISTS idx_setups_symbol ON setups (symbol);
CREATE INDEX IF NOT EXISTS idx_perf_setup_id ON performance_tracking (setup_id);
CREATE INDEX IF NOT EXISTS idx_regime_scan ON regime_snapshots (scan_id);
"""

# V1 Final Audit revision - "Data Collection Readiness" additive
# columns. Added via ALTER TABLE (same pattern already established in
# performance_tracker.py's _ensure_v1_1_columns) so pre-existing
# qde.db files from earlier revisions keep working without a manual
# migration step.
_ADDITIVE_SETUP_COLUMNS = {
    "price_at_scan": "REAL",
    "penalties_json": "TEXT",
    "contradictions_json": "TEXT",
    "expected_move_json": "TEXT",
    "expected_duration": "TEXT",
    "config_version": "TEXT",  # Deep Audit patch item #9
}

# Deep Audit patch (P1) - Fixed Horizon Outcome columns, additive to
# the pre-existing performance_tracking table rather than a brand new
# table, since this table's original purpose (Document 05 SS6-SS8) was
# always meant to hold exactly this kind of data - it was simply never
# wired up (Deep Audit finding A5).
_ADDITIVE_PERFORMANCE_COLUMNS = {
    "target_timestamp": "TEXT",
    "return_pct": "REAL",
    "high": "REAL",
    "low": "REAL",
    "completed": "INTEGER",
    "data_quality": "TEXT",
    "created_at": "TEXT",
}


class SQLiteRepository(SetupRepository):
    """SQLite-backed SetupRepository. See storage.repository for the
    contract this class must honor.
    """

    def __init__(self, db_path: str = "qde.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.logger = QDELogger.get(__name__)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._ensure_additive_columns()
            self._ensure_outcome_uniqueness_index()
            self._conn.commit()

    def _ensure_additive_columns(self) -> None:
        """Adds V1.1 columns to pre-existing tables if missing. Must be
        called while holding self._lock (matches how it's invoked from
        _init_schema).
        """
        existing_setup_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(setups)")}
        for column, sql_type in _ADDITIVE_SETUP_COLUMNS.items():
            if column not in existing_setup_cols:
                self._conn.execute(f"ALTER TABLE setups ADD COLUMN {column} {sql_type}")

        existing_perf_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(performance_tracking)")}
        for column, sql_type in _ADDITIVE_PERFORMANCE_COLUMNS.items():
            if column not in existing_perf_cols:
                self._conn.execute(f"ALTER TABLE performance_tracking ADD COLUMN {column} {sql_type}")

    def _ensure_outcome_uniqueness_index(self) -> None:
        """Deep Audit patch item #6: setup_id + horizon must be unique.
        A UNIQUE index (rather than a table-level constraint, since the
        table already existed pre-migration) enforces this at the
        database level as a defense-in-depth measure - insert_outcome()
        also enforces immutability at the application level (checked
        BEFORE this index would even be hit), so this index should
        never actually reject anything insert_outcome() didn't already
        refuse; it exists as a second line of defense against any
        future code path that might bypass insert_outcome().
        """
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_perf_setup_horizon_unique "
            "ON performance_tracking (setup_id, horizon)"
        )

    # -- SetupRepository implementation --------------------------------

    def insert_setup(
        self,
        result: Result,
        price_at_scan: float | None = None,
        config_version: str | None = None,
    ) -> None:
        setup = result.setup
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO setups (
                    setup_id, symbol, timestamp, market_regime, raw_score,
                    confidence, uniqueness, risk_grade, fingerprint_json,
                    feature_scores_json, passed, classification,
                    price_at_scan, penalties_json, contradictions_json,
                    expected_move_json, expected_duration, config_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    setup.setup_id,
                    setup.symbol,
                    setup.timestamp,
                    setup.market_regime,
                    setup.raw_score,
                    result.confidence,
                    result.uniqueness,
                    result.risk_grade,
                    json.dumps(setup.fingerprint),
                    json.dumps(setup.feature_scores),
                    int(setup.passed),
                    None,
                    price_at_scan,
                    json.dumps(setup.penalties),
                    json.dumps(result.contradictions),
                    json.dumps(result.expected_move.to_dict()),
                    result.expected_duration,
                    config_version,
                ),
            )
            self._conn.commit()
        self.logger.debug(f"Database write: setup {setup.setup_id} ({setup.symbol})")

    def update_performance(
        self,
        setup_id: str,
        horizon: str,
        price_at_horizon: float,
        mfe: float,
        mae: float,
    ) -> None:
        checked_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO performance_tracking (
                    setup_id, horizon, price_at_horizon, mfe, mae, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (setup_id, horizon, price_at_horizon, mfe, mae, checked_at),
            )
            self._conn.commit()
        self.logger.debug(
            f"Database write: performance {setup_id} @ {horizon} (mfe={mfe}, mae={mae})"
        )

    def get_setups_by_fingerprint(self, fingerprint: dict[str, str]) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM setups").fetchall()
        matches = []
        for row in rows:
            stored_fp = json.loads(row["fingerprint_json"] or "{}")
            if all(stored_fp.get(k) == v for k, v in fingerprint.items()):
                matches.append(self._row_to_dict(row))
        return matches

    def get_statistics(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        query = "SELECT * FROM setups WHERE passed = 1"
        params: list[Any] = []
        if filters and "market_regime" in filters:
            query += " AND market_regime = ?"
            params.append(filters["market_regime"])

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            setup_ids = [row["setup_id"] for row in rows]

            if not setup_ids:
                return {
                    "count": 0,
                    "win_rate": 0.0,
                    "avg_mfe": 0.0,
                    "avg_mae": 0.0,
                    "avg_confidence": 0.0,
                    "avg_score": 0.0,
                }

            placeholders = ",".join("?" * len(setup_ids))
            perf_rows = self._conn.execute(
                f"SELECT * FROM performance_tracking WHERE setup_id IN ({placeholders})",
                setup_ids,
            ).fetchall()

        mfes = [r["mfe"] for r in perf_rows if r["mfe"] is not None]
        maes = [r["mae"] for r in perf_rows if r["mae"] is not None]
        wins = [r["mfe"] for r in perf_rows if r["mfe"] is not None and r["mfe"] > 0]

        return {
            "count": len(rows),
            "win_rate": round(len(wins) / len(perf_rows), 4) if perf_rows else 0.0,
            "avg_mfe": round(sum(mfes) / len(mfes), 4) if mfes else 0.0,
            "avg_mae": round(sum(maes) / len(maes), 4) if maes else 0.0,
            "avg_confidence": round(
                sum(r["confidence"] for r in rows) / len(rows), 2
            ),
            "avg_score": round(sum(r["raw_score"] for r in rows) / len(rows), 2),
        }

    def get_recent_setups(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM setups ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def update_uniqueness(self, setup_id: str, uniqueness: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE setups SET uniqueness = ? WHERE setup_id = ?",
                (uniqueness, setup_id),
            )
            self._conn.commit()
        self.logger.debug(f"Database write: uniqueness corrected for setup {setup_id} -> {uniqueness}")

    def record_regime_snapshot(
        self,
        scan_id: str,
        timestamp: str,
        regime: str,
        btc_volatility_percentile: float | None,
        btc_bullish_alignment: bool | None,
        btc_bearish_alignment: bool | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO regime_snapshots (
                    scan_id, timestamp, regime, btc_volatility_percentile,
                    btc_bullish_alignment, btc_bearish_alignment
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    timestamp,
                    regime,
                    btc_volatility_percentile,
                    None if btc_bullish_alignment is None else int(btc_bullish_alignment),
                    None if btc_bearish_alignment is None else int(btc_bearish_alignment),
                ),
            )
            self._conn.commit()
        self.logger.debug(f"Database write: regime snapshot for scan {scan_id} -> {regime}")

    def start_scan_run(self, run_id: str, started_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO scan_runs (run_id, started_at) VALUES (?, ?)",
                (run_id, started_at),
            )
            self._conn.commit()

    def finish_scan_run(self, run_id: str, summary: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE scan_runs SET
                    finished_at = ?, symbols_scanned = ?, symbols_passed = ?,
                    avg_confidence = ?, avg_score = ?
                WHERE run_id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    summary.get("symbols_scanned"),
                    summary.get("symbols_passed"),
                    summary.get("avg_confidence"),
                    summary.get("avg_score"),
                    run_id,
                ),
            )
            self._conn.commit()
        self.logger.info(f"Database write: scan_run {run_id} finalized")

    # -- Fixed Horizon Outcome (Deep Audit patch P1) ---------------------

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
        with self._lock:
            existing = self._conn.execute(
                "SELECT completed FROM performance_tracking WHERE setup_id = ? AND horizon = ?",
                (setup_id, horizon),
            ).fetchone()
            if existing is not None and existing["completed"] == 1:
                # Immutability rule (patch item #6): a real, completed
                # outcome is never overwritten, even by a later call.
                self.logger.debug(
                    f"Outcome for {setup_id}@{horizon} already completed - insert_outcome() is a no-op"
                )
                return

            checked_at = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                """
                INSERT INTO performance_tracking (
                    setup_id, horizon, price_at_horizon, mfe, mae, checked_at,
                    target_timestamp, return_pct, high, low, completed,
                    data_quality, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(setup_id, horizon) DO UPDATE SET
                    price_at_horizon=excluded.price_at_horizon,
                    mfe=excluded.mfe, mae=excluded.mae, checked_at=excluded.checked_at,
                    target_timestamp=excluded.target_timestamp,
                    return_pct=excluded.return_pct, high=excluded.high, low=excluded.low,
                    completed=excluded.completed, data_quality=excluded.data_quality
                """,
                (
                    setup_id, horizon, target_price, mfe, mae, checked_at,
                    target_timestamp, return_pct, high, low, int(completed),
                    data_quality, checked_at,
                ),
            )
            self._conn.commit()
        self.logger.debug(
            f"Database write: outcome {setup_id}@{horizon} completed={completed} quality={data_quality}"
        )

    def get_outcome(self, setup_id: str, horizon: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM performance_tracking WHERE setup_id = ? AND horizon = ?",
                (setup_id, horizon),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        # Friendly alias: the column is named price_at_horizon for
        # backward compatibility with the pre-existing (unused)
        # update_performance() method's schema - callers of the new
        # Fixed Horizon Outcome system should read target_price.
        d["target_price"] = d.get("price_at_horizon")
        d["completed"] = bool(d.get("completed"))
        return d

    def get_pending_outcome_targets(self, as_of_iso: str, horizons: dict[str, int]) -> list[dict[str, Any]]:
        as_of = datetime.fromisoformat(as_of_iso)

        with self._lock:
            setup_rows = self._conn.execute(
                "SELECT setup_id, symbol, timestamp, price_at_scan FROM setups "
                "WHERE price_at_scan IS NOT NULL"
            ).fetchall()
            completed_rows = self._conn.execute(
                "SELECT setup_id, horizon FROM performance_tracking WHERE completed = 1"
            ).fetchall()

        already_completed = {(r["setup_id"], r["horizon"]) for r in completed_rows}

        pending: list[dict[str, Any]] = []
        for row in setup_rows:
            try:
                scan_time = datetime.fromisoformat(row["timestamp"])
            except ValueError:
                continue
            for horizon_name, horizon_seconds in horizons.items():
                if (row["setup_id"], horizon_name) in already_completed:
                    continue
                target_time = scan_time.timestamp() + horizon_seconds
                if target_time > as_of.timestamp():
                    continue  # not due yet
                pending.append(
                    {
                        "setup_id": row["setup_id"],
                        "symbol": row["symbol"],
                        "scan_timestamp": row["timestamp"],
                        "price_at_scan": row["price_at_scan"],
                        "horizon": horizon_name,
                        "target_timestamp": datetime.fromtimestamp(target_time, tz=timezone.utc).isoformat(),
                    }
                )
        return pending

    # -- internal helpers -------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["fingerprint"] = json.loads(d.pop("fingerprint_json") or "{}")
        d["feature_scores"] = json.loads(d.pop("feature_scores_json") or "{}")
        d["passed"] = bool(d["passed"])
        # V1.1 additive fields - guarded with `or "..."` since rows
        # written before this migration have NULL here, not an empty
        # JSON string.
        d["penalties"] = json.loads(d.pop("penalties_json", None) or "[]")
        d["contradictions"] = json.loads(d.pop("contradictions_json", None) or "[]")
        d["expected_move"] = json.loads(d.pop("expected_move_json", None) or "{}")
        return d

    def close(self) -> None:
        with self._lock:
            self._conn.close()
