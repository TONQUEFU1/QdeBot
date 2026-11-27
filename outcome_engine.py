"""
QDE V1.1 - outcome_engine.py

QDE Fixed Horizon Outcome Engine (standalone plugin module, Deep Audit
patch item P1).

PURPOSE
    Replaces the "current price at report-generation time" measurement
    that audit_engine.py used to compute realized returns (Deep Audit
    finding A3 - proven with real data: KAITO's current_price was
    bit-identical across 17 scans spanning 17 July - 5 August, because
    fetch_current_prices() is called once per report generation and
    applied to every historical row regardless of age) with REAL,
    immutable, historical-OHLCV-derived outcomes at fixed horizons:
    30m, 1h, 4h, 12h, 24h after each signal.

THE ONE ABSOLUTE RULE THIS MODULE FOLLOWS
    A signal's outcome for a given horizon is NEVER computed from a
    live/current price call. It is always computed from Binance's
    historical OHLCV data covering [scan_timestamp, target_timestamp],
    fetched via `since=scan_timestamp_ms` (data.exchange.ExchangeClient
    .get_ohlcv's new optional `since` parameter - added specifically
    for this module, zero change to any existing caller's behavior).
    If that historical data cannot be reliably obtained, the outcome
    is recorded as NOT completed with data_quality="missing_historical_data"
    - never silently substituted with a live price.

DETERMINISTIC HORIZON PRICE SELECTION
    scan_timestamp + horizon_seconds -> target_timestamp
    -> fetch 15m candles starting at scan_timestamp via `since`
    -> take every candle whose open time falls within
       [scan_timestamp, target_timestamp]
    -> target_price = close of the LAST such candle (the one covering
       target_timestamp) - a fixed, reproducible rule, not "whatever
       happened to be available when this ran".
    -> MFE = (max(high) over that window - entry_price) / entry_price
    -> MAE = (min(low) over that window - entry_price) / entry_price
    (LONG-only formulas - confirmed via source inspection that no
    SHORT/direction concept exists anywhere in engines/indicators/
    models before writing this module; QDE's scoring is structurally
    long-biased, so MFE=max-based/MAE=min-based is correct without
    guessing.)

IMMUTABILITY & DUPLICATE PREVENTION
    Enforced in storage/sqlite_repository.py's insert_outcome(): a
    (setup_id, horizon) pair that already has completed=1 is never
    overwritten by a later call - only genuinely pending or
    previously-missing-data rows can be (re)computed. A UNIQUE index
    on (setup_id, horizon) is a second line of defense at the DB level.

SCHEDULING
    No daemon required. process_pending_outcomes() is a plain async
    function, callable from anywhere QDE already runs code:
    standalone (`python outcome_engine.py`), or via main.py's new
    opt-in `--process-outcomes` flag (main.py wiring, not a forced
    behavior change to the default scan).

Usage:
    python outcome_engine.py                    # process all pending outcomes now
    python outcome_engine.py --max 50            # limit how many targets to process
    python outcome_engine.py --horizons 1h,24h   # restrict to specific horizons
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent

HORIZONS: dict[str, int] = {
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "12h": 43200,
    "24h": 86400,
}

OUTCOME_TIMEFRAME = "15m"
_TIMEFRAME_MS = {"15m": 15 * 60_000}


@dataclass
class OutcomeResult:
    completed: bool
    data_quality: str  # "ok" | "missing_historical_data" | "not_due_yet"
    target_timestamp: str | None = None
    target_price: float | None = None
    return_pct: float | None = None
    high: float | None = None
    low: float | None = None
    mfe: float | None = None
    mae: float | None = None


async def compute_outcome(
    exchange: Any,
    symbol: str,
    entry_price: float,
    scan_timestamp_iso: str,
    horizon_seconds: int,
) -> OutcomeResult:
    """Computes one signal's outcome for one horizon from real
    historical OHLCV data only - see module docstring's "ONE ABSOLUTE
    RULE". Never raises for a data problem; returns a
    completed=False/data_quality="missing_historical_data" result
    instead, per the explicit instruction to prefer missing data over
    wrong data.
    """
    try:
        scan_dt = datetime.fromisoformat(scan_timestamp_iso)
    except ValueError:
        return OutcomeResult(completed=False, data_quality="missing_historical_data")

    scan_ms = int(scan_dt.timestamp() * 1000)
    target_ms = scan_ms + horizon_seconds * 1000

    tf_ms = _TIMEFRAME_MS[OUTCOME_TIMEFRAME]
    candles_needed = int((target_ms - scan_ms) / tf_ms) + 5  # small buffer for edge rounding

    try:
        raw = await exchange.get_ohlcv(symbol, OUTCOME_TIMEFRAME, limit=candles_needed, since=scan_ms)
    except Exception:
        return OutcomeResult(completed=False, data_quality="missing_historical_data")

    if not raw:
        return OutcomeResult(completed=False, data_quality="missing_historical_data")

    window = [c for c in raw if scan_ms <= c[0] <= target_ms]
    if not window:
        return OutcomeResult(completed=False, data_quality="missing_historical_data")

    # A real gap check: if the window doesn't extend at least reasonably
    # close to target_ms, the exchange simply didn't have data that far
    # forward yet (e.g. a very new listing, or an outage) - flag rather
    # than silently accept a truncated window as if it were complete.
    last_candle_open = window[-1][0]
    coverage_gap_ms = target_ms - last_candle_open
    if coverage_gap_ms > tf_ms * 3:
        return OutcomeResult(completed=False, data_quality="missing_historical_data")

    target_candle = window[-1]
    target_price = float(target_candle[4])  # close
    highs = [float(c[2]) for c in window]
    lows = [float(c[3]) for c in window]

    if entry_price <= 0:
        return OutcomeResult(completed=False, data_quality="missing_historical_data")

    return OutcomeResult(
        completed=True,
        data_quality="ok",
        target_timestamp=datetime.fromtimestamp(target_candle[0] / 1000, tz=timezone.utc).isoformat(),
        target_price=round(target_price, 12),
        return_pct=round((target_price - entry_price) / entry_price * 100, 4),
        high=round(max(highs), 12),
        low=round(min(lows), 12),
        mfe=round((max(highs) - entry_price) / entry_price * 100, 4),
        mae=round((min(lows) - entry_price) / entry_price * 100, 4),
    )


@dataclass
class ProcessSummary:
    total_targets: int
    completed: int
    missing_data: int
    errors: int


async def process_pending_outcomes(
    repository: Any,
    exchange: Any | None = None,
    as_of: datetime | None = None,
    max_targets: int = 200,
    horizons: dict[str, int] | None = None,
) -> ProcessSummary:
    """Finds every (setup, horizon) pair whose target_timestamp has
    already passed and which has no completed outcome yet, computes
    each from real historical OHLCV, and persists the result.

    Safe to call repeatedly (e.g. once per --ai-audit run, or via its
    own CLI on a schedule) - already-completed pairs are skipped
    entirely (repository.get_pending_outcome_targets already excludes
    them), and insert_outcome() additionally refuses to overwrite a
    completed row even if somehow re-attempted.
    """
    horizons = horizons or HORIZONS
    as_of_iso = (as_of or datetime.now(timezone.utc)).isoformat()

    targets = repository.get_pending_outcome_targets(as_of_iso, horizons)
    targets = targets[:max_targets]

    if not targets:
        return ProcessSummary(total_targets=0, completed=0, missing_data=0, errors=0)

    owns_exchange = exchange is None
    if owns_exchange:
        from data.exchange import ExchangeClient

        exchange = ExchangeClient(rate_limit_config={"requests_per_second": 8.0, "max_retries": 3, "burst": 50})

    completed_count = 0
    missing_count = 0
    error_count = 0
    try:
        for target in targets:
            try:
                result = await compute_outcome(
                    exchange,
                    target["symbol"],
                    target["price_at_scan"],
                    target["scan_timestamp"],
                    horizons[target["horizon"]],
                )
                repository.insert_outcome(
                    setup_id=target["setup_id"],
                    horizon=target["horizon"],
                    target_timestamp=result.target_timestamp,
                    target_price=result.target_price,
                    return_pct=result.return_pct,
                    high=result.high,
                    low=result.low,
                    mfe=result.mfe,
                    mae=result.mae,
                    completed=result.completed,
                    data_quality=result.data_quality,
                )
                if result.completed:
                    completed_count += 1
                else:
                    missing_count += 1
            except Exception:
                error_count += 1
    finally:
        if owns_exchange:
            await exchange.close()

    return ProcessSummary(
        total_targets=len(targets),
        completed=completed_count,
        missing_data=missing_count,
        errors=error_count,
    )


def _get_default_repository():
    from storage.sqlite_repository import SQLiteRepository

    return SQLiteRepository(db_path=str(_ROOT / "qde.db"))


async def run(max_targets: int = 200, horizon_filter: list[str] | None = None) -> str:
    horizons = HORIZONS
    if horizon_filter:
        horizons = {k: v for k, v in HORIZONS.items() if k in horizon_filter}
        if not horizons:
            return f"Gecersiz horizon(lar): {horizon_filter}. Gecerli degerler: {list(HORIZONS.keys())}"

    repo = _get_default_repository()
    try:
        summary = await process_pending_outcomes(repo, max_targets=max_targets, horizons=horizons)
    finally:
        repo.close()

    lines = [
        "=" * 44,
        "OUTCOME ENGINE - FIXED HORIZON SONUCLARI",
        "=" * 44,
        f"Islenen hedef sayisi : {summary.total_targets}",
        f"Tamamlanan (gercek)  : {summary.completed}",
        f"Veri eksik           : {summary.missing_data}",
        f"Hata                 : {summary.errors}",
        "=" * 44,
    ]
    if summary.total_targets == 0:
        lines.append("Su an vadesi gelmis, bekleyen bir sinyal outcome'u yok.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="QDE Fixed Horizon Outcome Engine")
    parser.add_argument("--max", type=int, default=200, help="Bu calistirmada islenecek maksimum hedef sayisi")
    parser.add_argument("--horizons", type=str, default=None, help="Virgulle ayrilmis horizon listesi, orn: 1h,24h")
    args = parser.parse_args()
    horizon_filter = args.horizons.split(",") if args.horizons else None
    print(asyncio.run(run(max_targets=args.max, horizon_filter=horizon_filter)))


if __name__ == "__main__":
    main()
