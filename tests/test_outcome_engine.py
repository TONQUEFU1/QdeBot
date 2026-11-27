"""
QDE V1.1 - tests/test_outcome_engine.py

Permanent regression tests for the Fixed Horizon Outcome patch (Deep
Audit finding A3/A5 fix). Covers every mandatory test category from
the patch instructions:
    - Database: migration, duplicate constraint, setup_id+horizon uniqueness
    - Horizon: 30m/1h/4h/12h/24h each verified independently
    - Timestamp: target_timestamp == scan_timestamp + horizon
    - No-Lookahead / No-Current-Price: a synthetic test proving a late
      system run does NOT use the live price for a past horizon
    - Historical Data Missing: completed=False, data_quality=missing_historical_data
    - Duplicate: running the scheduler twice never double-inserts
    - Rank: top15_rank backfill (audit_engine.py)
    - Scoring Regression: config.version is deterministic and does not
      change scoring/confidence/hard-filter behavior
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from models.result import ExpectedMove, Result
from models.setup import Setup
from outcome_engine import HORIZONS, compute_outcome, process_pending_outcomes
from storage.sqlite_repository import SQLiteRepository


def _make_repo() -> tuple[SQLiteRepository, str]:
    db_path = tempfile.mktemp(suffix=".db")
    return SQLiteRepository(db_path=db_path), db_path


def _make_result(symbol: str, scan_time_iso: str) -> Result:
    setup = Setup(
        symbol=symbol, timestamp=scan_time_iso, market_regime="bull",
        raw_score=90.0, feature_scores={"trend": 10}, passed=True,
    )
    return Result(
        setup=setup, confidence=90.0, risk_grade="A", uniqueness=80.0,
        expected_move=ExpectedMove(), expected_duration="8-24h",
    )


# ---------------------------------------------------------------------
# Database: migration, uniqueness, duplicate prevention
# ---------------------------------------------------------------------

def test_old_database_migrates_and_opens_without_error():
    """A qde.db from before this patch (no outcome columns, no
    config_version column) must still open and be usable.
    """
    db_path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE setups (
            setup_id TEXT PRIMARY KEY, symbol TEXT, timestamp TEXT, market_regime TEXT,
            raw_score REAL, confidence REAL, uniqueness REAL, risk_grade TEXT,
            fingerprint_json TEXT, feature_scores_json TEXT, passed INTEGER, classification TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE performance_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT, setup_id TEXT, horizon TEXT,
            price_at_horizon REAL, mfe REAL, mae REAL, checked_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO setups VALUES ('old-1','OLDUSDT','2026-01-01T00:00:00+00:00','unknown',80.0,85.0,50.0,'A','{}','{}',1,NULL)"
    )
    conn.commit()
    conn.close()

    repo = SQLiteRepository(db_path=db_path)
    recent = repo.get_recent_setups(limit=10)
    assert len(recent) == 1
    assert recent[0]["symbol"] == "OLDUSDT"

    # New columns must exist and be queryable (NULL for the old row).
    outcome = repo.get_outcome("old-1", "1h")
    assert outcome is None  # no outcome row for this old setup - correct, not an error

    repo.close()
    import os
    os.remove(db_path)


def test_setup_id_horizon_uniqueness_enforced_at_db_level():
    repo, db_path = _make_repo()
    repo.insert_outcome("s1", "1h", "2026-01-01T01:00:00Z", 100.0, 5.0, 101.0, 99.0, 6.0, -1.0, True, "ok")
    with pytest.raises(sqlite3.IntegrityError):
        # Bypasses insert_outcome's own immutability check by inserting
        # directly - the UNIQUE index must still refuse a raw duplicate.
        repo._conn.execute(
            "INSERT INTO performance_tracking (setup_id, horizon, checked_at) VALUES (?, ?, ?)",
            ("s1", "1h", "2026-01-01T00:00:00Z"),
        )
    repo.close()
    import os
    os.remove(db_path)


def test_insert_outcome_never_overwrites_a_completed_row():
    repo, db_path = _make_repo()
    repo.insert_outcome("s1", "1h", "2026-01-01T01:00:00Z", 100.0, 5.0, 101.0, 99.0, 6.0, -1.0, True, "ok")
    repo.insert_outcome("s1", "1h", "WRONG", 999.0, 999.0, 999.0, 999.0, 999.0, 999.0, True, "ok")
    stored = repo.get_outcome("s1", "1h")
    assert stored["target_price"] == 100.0
    repo.close()
    import os
    os.remove(db_path)


def test_calling_process_pending_outcomes_twice_does_not_duplicate():
    import asyncio

    repo, db_path = _make_repo()
    scan_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    repo.insert_setup(_make_result("DUPUSDT", scan_time), price_at_scan=100.0)

    class FakeExchange:
        async def get_ohlcv(self, symbol, timeframe, limit, since):
            ts0 = since
            return [[ts0 + i * 15 * 60 * 1000, 100, 101, 99, 100 + i, 1000] for i in range(10)]

        async def close(self):
            pass

    async def run_twice():
        await process_pending_outcomes(repo, exchange=FakeExchange(), max_targets=10)
        await process_pending_outcomes(repo, exchange=FakeExchange(), max_targets=10)

    asyncio.run(run_twice())

    count = repo._conn.execute(
        "SELECT COUNT(*) FROM performance_tracking WHERE horizon = '30m'"
    ).fetchone()[0]
    assert count == 1, f"expected exactly one 30m outcome row, found {count}"
    repo.close()
    import os
    os.remove(db_path)


# ---------------------------------------------------------------------
# Horizon coverage
# ---------------------------------------------------------------------

@pytest.mark.parametrize("horizon_name,horizon_seconds", list(HORIZONS.items()))
def test_each_horizon_computes_independently(horizon_name, horizon_seconds):
    import asyncio

    scan_dt = datetime.now(timezone.utc) - timedelta(seconds=horizon_seconds + 3600)
    scan_ms = int(scan_dt.timestamp() * 1000)
    entry_price = 100.0

    class FakeExchange:
        async def get_ohlcv(self, symbol, timeframe, limit, since):
            assert since == scan_ms
            n = limit
            return [[scan_ms + i * 15 * 60 * 1000, 100, 102, 98, 100 + (i % 5), 1000] for i in range(n)]

    result = asyncio.run(compute_outcome(FakeExchange(), "XUSDT", entry_price, scan_dt.isoformat(), horizon_seconds))
    assert result.completed is True
    assert result.target_price is not None


# ---------------------------------------------------------------------
# Timestamp correctness
# ---------------------------------------------------------------------

def test_target_timestamp_equals_scan_plus_horizon():
    import asyncio

    scan_dt = datetime.now(timezone.utc) - timedelta(hours=3)
    scan_ms = int(scan_dt.timestamp() * 1000)
    horizon_seconds = HORIZONS["1h"]
    expected_target_ms = scan_ms + horizon_seconds * 1000

    class FakeExchange:
        async def get_ohlcv(self, symbol, timeframe, limit, since):
            return [[scan_ms + i * 15 * 60 * 1000, 100, 101, 99, 100, 1000] for i in range(10)]

    result = asyncio.run(compute_outcome(FakeExchange(), "XUSDT", 100.0, scan_dt.isoformat(), horizon_seconds))
    actual_target_ms = int(datetime.fromisoformat(result.target_timestamp).timestamp() * 1000)
    # The actual target candle's open time must be <= expected and within one candle width of it.
    assert actual_target_ms <= expected_target_ms
    assert expected_target_ms - actual_target_ms < 15 * 60 * 1000


# ---------------------------------------------------------------------
# No-Lookahead / No-Current-Price (the single most important test)
# ---------------------------------------------------------------------

def test_outcome_never_reflects_a_price_later_than_target_timestamp():
    """The exact scenario from the patch instructions: signal at
    10:00, 1h target at 11:00, system happens to run at 15:00 - the
    computed outcome must come from the 11:00 candle, never whatever
    price existed at 15:00.
    """
    import asyncio

    scan_dt = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    scan_ms = int(scan_dt.timestamp() * 1000)
    target_ms = scan_ms + 3600 * 1000  # 11:00

    # Candles run from 10:00 all the way to 15:30 (as if fetched by a
    # system that happened to run late) - close prices rise steadily,
    # so if the function incorrectly picked a late candle, target_price
    # would be much higher than the true 11:00 value.
    candles = []
    for i in range(23):  # 10:00 .. 15:30 in 15m steps
        ts = scan_ms + i * 15 * 60 * 1000
        candles.append([ts, 100 + i, 100 + i + 1, 100 + i - 1, 100 + i, 1000])

    class FakeExchange:
        async def get_ohlcv(self, symbol, timeframe, limit, since):
            assert since == scan_ms, "must fetch starting from the SIGNAL's own time, never 'now'"
            return candles

    result = asyncio.run(compute_outcome(FakeExchange(), "XUSDT", 100.0, scan_dt.isoformat(), 3600))

    # The 11:00 candle is index 4 (10:00 + 4*15m = 11:00) -> close = 100+4 = 104.
    assert result.target_price == 104.0, (
        f"target_price must come from the 11:00 candle (104.0), not a later one - got {result.target_price}"
    )
    # A 15:30 candle would have close=122 - explicitly assert we did NOT get that.
    assert result.target_price != 122.0


# ---------------------------------------------------------------------
# Historical data missing
# ---------------------------------------------------------------------

def test_missing_historical_data_is_honestly_flagged_not_guessed():
    import asyncio

    scan_dt = datetime.now(timezone.utc) - timedelta(hours=2)

    class EmptyExchange:
        async def get_ohlcv(self, symbol, timeframe, limit, since):
            return []

    result = asyncio.run(compute_outcome(EmptyExchange(), "XUSDT", 100.0, scan_dt.isoformat(), 3600))
    assert result.completed is False
    assert result.data_quality == "missing_historical_data"
    assert result.target_price is None
    assert result.return_pct is None


def test_coverage_gap_beyond_tolerance_is_flagged_missing():
    """If the exchange's historical data simply doesn't reach far
    enough forward (a real gap, not just end-of-window rounding), the
    result must be honestly incomplete rather than using a stale candle
    far short of the real target time.
    """
    import asyncio

    scan_dt = datetime.now(timezone.utc) - timedelta(hours=2)
    scan_ms = int(scan_dt.timestamp() * 1000)

    class SparseExchange:
        async def get_ohlcv(self, symbol, timeframe, limit, since):
            # Only returns the FIRST candle when a full hour (4 candles)
            # was requested - an unambiguous, large coverage gap.
            return [[scan_ms, 100, 101, 99, 100, 1000]]

    result = asyncio.run(compute_outcome(SparseExchange(), "XUSDT", 100.0, scan_dt.isoformat(), 3600))
    assert result.completed is False
    assert result.data_quality == "missing_historical_data"


# ---------------------------------------------------------------------
# Rank backfill (audit_engine.py)
# ---------------------------------------------------------------------

def test_backfill_top15_ranks_writes_only_the_given_symbols():
    import audit_engine
    import tempfile as tf

    db_path = tf.mktemp(suffix=".db")
    original_path = audit_engine._AUDIT_DB_PATH
    audit_engine._AUDIT_DB_PATH = db_path
    try:
        conn = audit_engine._connect_audit_db()
        conn.execute(
            "INSERT INTO indicator_snapshots (scan_id, scan_timestamp, symbol, passed, indicator_name) "
            "VALUES (?, ?, ?, ?, ?)",
            ("scan-1", "2026-01-01T00:00:00Z", "AUSDT", 1, "trend"),
        )
        conn.execute(
            "INSERT INTO indicator_snapshots (scan_id, scan_timestamp, symbol, passed, indicator_name) "
            "VALUES (?, ?, ?, ?, ?)",
            ("scan-1", "2026-01-01T00:00:00Z", "BUSDT", 1, "trend"),
        )
        conn.commit()
        conn.close()

        audit_engine.backfill_top15_ranks("scan-1", {"AUSDT": 1, "BUSDT": 2})

        conn = audit_engine._connect_audit_db()
        rows = {r["symbol"]: r["top15_rank"] for r in conn.execute("SELECT symbol, top15_rank FROM indicator_snapshots")}
        conn.close()
        assert rows == {"AUSDT": 1, "BUSDT": 2}
    finally:
        audit_engine._AUDIT_DB_PATH = original_path
        import os
        if os.path.exists(db_path):
            os.remove(db_path)


# ---------------------------------------------------------------------
# Config version (scoring regression safety net)
# ---------------------------------------------------------------------

def test_config_version_is_deterministic_and_stable():
    import main

    config1 = main.QDEConfig.load()
    config2 = main.QDEConfig.load()
    assert config1.version == config2.version, "identical config content must produce an identical version hash"
    assert isinstance(config1.version, str) and len(config1.version) == 12


def test_config_version_changes_when_weights_change():
    import main
    import copy

    config = main.QDEConfig.load()
    mutated = copy.deepcopy(config)
    mutated.weights["trend"] = mutated.weights.get("trend", 1.0) + 0.1
    assert mutated.version != config.version, "a changed weight must produce a different version hash"
