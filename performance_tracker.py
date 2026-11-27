"""
QDE V1.1 - performance_tracker.py

QDE Performance Tracker (standalone plugin module).

RULES THIS MODULE FOLLOWS (per explicit request):
    - Never modifies the scanning engine, Hard Filter, Scoring Engine,
      Confidence Engine, or terminal scan output in any way.
    - Never triggers a new 500+ symbol scan.
    - Uses its own separate SQLite database (performance.db) - does not
      touch qde.db or the SetupRepository/Setup/Result schema at all.
    - Only ever fetches live prices for the small set of symbols that
      already appear in previously recorded Top 15 snapshots.

INTEGRATION POINTS (the only two places this module touches the rest
of the codebase):
    1. main.py calls `record_top15(results, pipeline.exchange)` once,
       right after a scan finishes - wrapped in try/except so a
       tracker failure can never break the actual scan.
    2. A tiny CLI entrypoint here (`python performance_tracker.py
       <amount> <gün|hafta|ay>`) so a `rapor` shell alias can generate
       reports without running main.py at all.

Usage:
    # Recording (called automatically by main.py after every scan):
    await record_top15(results, exchange)

    # Reporting (run directly, or via a shell alias - see bottom of file):
    python performance_tracker.py 1 gün
    python performance_tracker.py 3 gün
    python performance_tracker.py 1 hafta
    python performance_tracker.py 1 ay
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DB_PATH = Path(__file__).resolve().parent / "performance.db"
_TOP_N = 15

_SCHEMA = """
CREATE TABLE IF NOT EXISTS top15_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    scan_timestamp TEXT NOT NULL,
    rank INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    score REAL,
    confidence REAL,
    grade TEXT,
    category TEXT,
    price_at_scan REAL,
    setup_id TEXT,
    feature_scores_json TEXT
);

CREATE TABLE IF NOT EXISTS signal_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    scan_timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_top15_timestamp ON top15_snapshots (scan_timestamp);
CREATE INDEX IF NOT EXISTS idx_top15_symbol ON top15_snapshots (symbol);
"""

_UNIT_DAYS = {"gün": 1, "gun": 1, "hafta": 7, "ay": 30}


def _ensure_v1_1_columns(conn: sqlite3.Connection) -> None:
    """Additive migration for pre-existing performance.db files created
    before setup_id/feature_scores_json existed. Safe to call every
    time - checks first, never errors on an already-migrated database.
    Needed by learning_engine.py, which correlates each indicator's
    individual contribution (feature_scores) with realized outcome -
    something the original schema had no way to capture.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(top15_snapshots)")}
    if "setup_id" not in existing:
        conn.execute("ALTER TABLE top15_snapshots ADD COLUMN setup_id TEXT")
    if "feature_scores_json" not in existing:
        conn.execute("ALTER TABLE top15_snapshots ADD COLUMN feature_scores_json TEXT")
    conn.commit()


# ---------------------------------------------------------------------
# Recording (called from main.py after a scan finishes)
# ---------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _ensure_v1_1_columns(conn)
    return conn


async def record_top15(results: list, exchange: Any, db_path: Path = _DB_PATH) -> None:
    """Record the Top 15 passed signals from a completed scan.

    `results` is the same ranked list[Result] main.py already produces
    and prints - this function does not re-rank or re-filter anything,
    it just takes the first 15 already-passed entries, exactly what
    the terminal's "Top Signals" section shows.

    `exchange` is the pipeline's already-open ExchangeClient - reused
    here so this function opens zero new network connections; it only
    issues up to 15 extra ticker requests, once, after the scan.
    """
    passed = [r for r in results if r.setup.passed]
    top15 = passed[:_TOP_N]
    if not top15:
        return

    scan_id = str(uuid.uuid4())
    scan_timestamp = datetime.now(timezone.utc).isoformat()

    rows = []
    for rank, result in enumerate(top15, start=1):
        try:
            ticker = await exchange.get_ticker(result.symbol)
            price = ticker.get("last")
        except Exception:
            price = None

        # Import kept local and optional: category() is a small, pure
        # labeling helper in output.terminal (Institutional Grade / High
        # Probability / etc.) - reused here only for display consistency
        # with what was actually printed. If unavailable for any reason,
        # recording still proceeds without it rather than failing.
        try:
            from output.terminal import category_label
            category = category_label(result)
        except Exception:
            category = ""

        rows.append(
            (
                scan_id,
                scan_timestamp,
                rank,
                result.symbol,
                result.score,
                result.confidence,
                result.risk_grade,
                category,
                price,
                result.setup.setup_id,
                json.dumps(result.setup.feature_scores),
            )
        )

    conn = _connect()
    try:
        conn.executemany(
            """
            INSERT INTO top15_snapshots (
                scan_id, scan_timestamp, rank, symbol, score,
                confidence, grade, category, price_at_scan,
                setup_id, feature_scores_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        # Her sinyali ayri bir tabloya kaydet (sayim icin)
        signal_rows = [(scan_id, r[3], scan_timestamp) for r in rows]
        conn.executemany(
            "INSERT INTO signal_counts (scan_id, symbol, scan_timestamp) VALUES (?, ?, ?)",
            signal_rows,
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Reporting (CLI entrypoint - `rapor 1 hafta` etc.)
# ---------------------------------------------------------------------

@dataclass
class PeriodSpec:
    start: datetime
    label: str


def parse_period(tokens: list[str]) -> PeriodSpec:
    """Parses ["1","gün"] / ["3","gün"] / ["1","hafta"] / ["1","ay"]
    (an optional leading "rapor" token is ignored so both
    `rapor 1 hafta` and `1 hafta` work).
    """
    cleaned = [t.lower() for t in tokens if t.lower() != "rapor"]
    if len(cleaned) < 2:
        raise ValueError("Kullanım: rapor <sayı> <gün|hafta|ay>  (örn: rapor 1 hafta)")

    try:
        amount = int(cleaned[0])
    except ValueError:
        raise ValueError(f"Geçersiz sayı: '{cleaned[0]}'")

    unit = cleaned[1]
    if unit not in _UNIT_DAYS:
        raise ValueError(f"Geçersiz birim: '{unit}' (gün, hafta veya ay olmalı)")

    days = amount * _UNIT_DAYS[unit]
    unit_label = {"gün": "Gün", "gun": "Gün", "hafta": "Hafta", "ay": "Ay"}[unit]
    label = f"Son {amount} {unit_label}"
    start = datetime.now(timezone.utc) - timedelta(days=days)
    return PeriodSpec(start=start, label=label)


def fetch_snapshots(period_start: datetime, db_path: Path = _DB_PATH) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM top15_snapshots WHERE scan_timestamp >= ? ORDER BY scan_timestamp, rank",
            (period_start.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def fetch_current_prices(symbols: list[str]) -> dict[str, float | None]:
    """Fetches the current price for exactly these symbols - never a
    fresh 530-symbol scan. Opens and closes its own short-lived
    ExchangeClient since the CLI report runs independently of main.py.

    Prints lightweight progress every 20 symbols - after weeks of
    accumulated Top15 history (performance_tracker's own db), this
    list can grow into the hundreds of unique symbols, and fetching
    them one by one over the real network can take a noticeable amount
    of time with zero terminal output otherwise - which looks
    indistinguishable from a hang. This is display-only; it does not
    change what's fetched or how.
    """
    from data.exchange import ExchangeClient

    exchange = ExchangeClient(rate_limit_config={"requests_per_second": 8.0, "max_retries": 3})
    prices: dict[str, float | None] = {}
    total = len(symbols)
    if total > 20:
        print(f"Guncel fiyatlar cekiliyor: {total} sembol...")
    try:
        for i, symbol in enumerate(symbols, start=1):
            try:
                ticker = await exchange.get_ticker(symbol)
                prices[symbol] = ticker.get("last")
            except Exception:
                prices[symbol] = None
            if total > 20 and i % 20 == 0:
                print(f"  {i}/{total} sembol tamamlandi...")
    finally:
        await exchange.close()
    if total > 20:
        print(f"Fiyat cekme tamamlandi: {total}/{total}")
    return prices


def pct_change(entry_price: float | None, current_price: float | None) -> float | None:
    """Public: shared with learning_engine.py so both modules compute
    returns identically. Same logic performance_tracker's own report
    already used privately.
    """
    if not entry_price or current_price is None:
        return None
    return round((current_price - entry_price) / entry_price * 100, 4)


# Backward-compatible private alias (kept in case anything imported the
# old private name directly).
_pct_change = pct_change


def build_report(snapshots: list[dict], current_prices: dict[str, float | None], period_label: str) -> str:
    """Sadece en cok sinyal alan 15 coini terminale basar."""
    lines: list[str] = []
    
    symbol_counts = {}
    for s in snapshots:
        sym = s["symbol"].replace("/USDT:USDT", "").replace("USDT", "")
        symbol_counts[sym] = symbol_counts.get(sym, 0) + 1
    
    top_15 = sorted(symbol_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    
    lines.append("=" * 50)
    lines.append(f"QDE PERFORMANS RAPORU - {period_label}")
    lines.append("=" * 50)
    lines.append("")
    lines.append("EN COK SINYAL ALAN 15 COIN:")
    lines.append("-" * 50)
    
    for i, (sym, count) in enumerate(top_15, start=1):
        lines.append(f"{i:2d}. #{sym:<12} -> {count:>3} kez Top15'te")
    
    lines.append("")
    lines.append("=" * 50)
    lines.append("(Detayli istatistikler arka planda kaydedildi.)")
    lines.append("=" * 50)
    
    return chr(10).join(lines)


async def generate_report(tokens: list[str]) -> str:
    period = parse_period(tokens)
    snapshots = fetch_snapshots(period.start)
    if not snapshots:
        return f"'{period.label}' icin kayitli Top15 taramasi bulunamadi."

    symbols = sorted(set(s["symbol"] for s in snapshots))
    current_prices = await fetch_current_prices(symbols)
    return build_report(snapshots, current_prices, period.label)


def main() -> None:
    tokens = sys.argv[1:]
    if not tokens:
        print("Kullanim: python performance_tracker.py <sayi> <gun|hafta|ay>")
        print("Ornek   : python performance_tracker.py 1 hafta")
        return
    try:
        report = asyncio.run(generate_report(tokens))
    except ValueError as exc:
        print(f"Hata: {exc}")
        return
    print(report, flush=True)



async def generate_report(tokens: list[str]) -> str:
    period = parse_period(tokens)
    snapshots = fetch_snapshots(period.start)
    if not snapshots:
        return f"'{period.label}' icin kayitli Top15 taramasi bulunamadi."

    symbols = sorted(set(s["symbol"] for s in snapshots))
    current_prices = await fetch_current_prices(symbols)
    return build_report(snapshots, current_prices, period.label)


def main() -> None:
    tokens = sys.argv[1:]
    if not tokens:
        print("Kullanim: python performance_tracker.py <sayi> <gun|hafta|ay>")
        print("Ornek   : python performance_tracker.py 1 hafta")
        return
    try:
        report = asyncio.run(generate_report(tokens))
    except ValueError as exc:
        print(f"Hata: {exc}")
        return
    print(report, flush=True)


if __name__ == "__main__":
    main()
