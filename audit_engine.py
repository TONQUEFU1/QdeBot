"""
QDE V1.1 - audit_engine.py

QDE AI Audit Engine (standalone plugin module).

PURPOSE
    Produces a single ZIP package containing every piece of real data
    QDE has ever recorded, formatted so an external LLM (Claude,
    ChatGPT, Gemini, etc.) can perform a black-box AND white-box audit
    of the scanning system: why signals fired, why they didn't, which
    filters/indicators are pulling weight, and where the architecture
    itself has gaps.

HONESTY POLICY (read this before touching the file list below)
    Several data points commonly wanted in an audit like this - macro
    market context (BTC Dominance, Fear&Greed, TOTAL2/3, Altseason),
    raw sub-indicator values (ATR, ADX, MACD, VWAP, Bollinger bands,
    candle-pattern/fake-breakout detection), and fixed-horizon
    (30m/1h/4h/12h/24h) forward performance tracking - are NOT
    currently computed or collected anywhere in this codebase. Rather
    than fabricate placeholder zeros for them (which would actively
    mislead whatever AI reads this package), this module:
        1. Includes only real, actually-recorded data in every file.
        2. Generates 16_missing_data_report.{txt,json} - a first-class,
           structured account of exactly what's missing, why it would
           help, how much engineering it needs, and in what priority
           order it should be added. See MISSING_METRICS below.
    Feeding an auditor honest gaps is more valuable than feeding it
    fabricated completeness.

RULES THIS MODULE FOLLOWS (per explicit request)
    - Never modifies engines/scoring.py, engines/confidence.py,
      engines/hard_filter.py, indicators/*, storage/repository.py, or
      any other core module or its interface.
    - Capture is opt-in (--audit-capture / --ai-audit in main.py), off
      by default, isolated in try/except so a capture failure can
      never break a real scan.
    - Uses the existing SetupRepository abstraction (via
      SQLiteRepository) to read qde.db - never a new, competing
      persistence interface for data that's already stored properly.
    - Only ever WRITES to its own audit.db and the output ZIP - never
      to qde.db, performance.db, or config/weights.json.

WHAT GOES FORWARD-ONLY VS. WHAT'S AVAILABLE RETROACTIVELY
    02_all_setups.csv, 07_feature_scores.json, 11_weight_history.json,
    13_system_config.json, 14_statistics.json, and the Top15 history
    all draw on data QDE already persists today (qde.db,
    performance.db, learning_log.json, config/*.json) - these are
    complete as soon as you run this, even on a fresh install with
    prior scan history.
    04_indicator_snapshot.csv, 06_filter_decisions.csv,
    09_false_positive.csv, and 10_false_negative.csv need
    price-at-scan-time and full per-indicator metadata for EVERY
    scanned symbol (not just the Top 15) - data qde.db was never
    designed to store. This module adds that capture (audit.db),
    but only from the point --audit-capture is first used onward.

Usage:
    # From main.py (recommended): runs a real scan with capture on,
    # then packages everything automatically.
    python main.py --ai-audit

    # Capture only (no package yet) during a normal scan:
    python main.py --audit-capture

    # Package whatever has already been captured, without scanning:
    python audit_engine.py
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import statistics
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_AUDIT_DB_PATH = _ROOT / "audit.db"
_OUTPUT_DIR = _ROOT / "reports"

_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS indicator_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    scan_timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price_at_scan REAL,
    passed INTEGER NOT NULL,
    raw_score REAL,
    confidence REAL,
    risk_grade TEXT,
    top15_rank INTEGER,
    indicator_name TEXT NOT NULL,
    feature_value REAL,
    feature_score REAL,
    feature_status TEXT,
    feature_family TEXT,
    feature_metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS filter_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    scan_timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    passed INTEGER NOT NULL,
    rejection_reasons_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ind_snap_scan ON indicator_snapshots (scan_id);
CREATE INDEX IF NOT EXISTS idx_ind_snap_symbol ON indicator_snapshots (symbol);
CREATE INDEX IF NOT EXISTS idx_filter_dec_scan ON filter_decisions (scan_id);
"""


def _run_async_safely(coro):
    """CRITICAL BUG FIX (confirmed against a real production audit
    package where every one of 660 Top15 history rows had an empty
    current_price/pct_change): `asyncio.run(coro)` raises
    `RuntimeError: asyncio.run() cannot be called from a running event
    loop` when this module's generate_package() is invoked via
    `python main.py --ai-audit` - main.py calls generate_package()
    synchronously from inside main_async(), which is itself already
    running under asyncio.run(). The narrow `except Exception:` around
    the old asyncio.run() call silently swallowed that RuntimeError
    and fell back to an all-None price dict, corrupting every
    downstream statistic (avg_realized_return_pct, win_rate_pct,
    False Positive/Negative reports) without any visible error.

    This helper detects whether a loop is already running and, if so,
    runs the coroutine to completion on a separate thread with its own
    fresh event loop instead - safe to call whether generate_package()
    is invoked standalone (`python audit_engine.py`, no parent loop)
    or from within main.py's --ai-audit (parent loop already running).
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop running in this thread - the simple, common case.
        return asyncio.run(coro)

    result_holder: dict[str, Any] = {}
    error_holder: dict[str, BaseException] = {}

    def _run_in_new_loop() -> None:
        try:
            result_holder["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            error_holder["error"] = exc

    thread = threading.Thread(target=_run_in_new_loop, daemon=True)
    thread.start()
    thread.join()

    if "error" in error_holder:
        raise error_holder["error"]
    return result_holder["value"]


def _connect_audit_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_AUDIT_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_AUDIT_SCHEMA)
    return conn


# ---------------------------------------------------------------------
# Capture (called from main.py's _process_one, opt-in, try/except-isolated)
# ---------------------------------------------------------------------

def capture_snapshot(
    scan_id: str,
    scan_timestamp: str,
    symbol: str,
    market_data: Any,
    features: list,
    setup: Any,
    passed: bool,
    reasons: list[str],
    top15_rank: int | None = None,
) -> None:
    """Records everything QDE actually computed for one symbol in one
    scan: every indicator's raw value/score/status/family/metadata,
    the Hard Filter's exact pass/fail reasons, and the price at scan
    time (needed later for real False Positive / False Negative
    analysis - see module docstring).

    Safe to call unconditionally from main.py; all failure handling
    (isolating this from the real scan) happens at the call site, per
    the same pattern already used for performance_tracker.py.
    """
    price_at_scan = market_data.ticker.get("last") or market_data.mark_price.get("mark_price")

    conn = _connect_audit_db()
    try:
        conn.execute(
            """
            INSERT INTO filter_decisions (scan_id, scan_timestamp, symbol, passed, rejection_reasons_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scan_id, scan_timestamp, symbol, int(passed), json.dumps(reasons)),
        )
        rows = [
            (
                scan_id,
                scan_timestamp,
                symbol,
                price_at_scan,
                int(passed),
                getattr(setup, "raw_score", None),
                None,  # confidence: not known at this pipeline stage yet (Confidence Engine runs after)
                None,  # risk_grade: same
                top15_rank,
                f.name,
                f.value,
                f.score,
                f.status.value if hasattr(f.status, "value") else str(f.status),
                f.family.value if hasattr(f.family, "value") else str(f.family),
                json.dumps(f.metadata),
            )
            for f in features
        ]
        conn.executemany(
            """
            INSERT INTO indicator_snapshots (
                scan_id, scan_timestamp, symbol, price_at_scan, passed,
                raw_score, confidence, risk_grade, top15_rank,
                indicator_name, feature_value, feature_score,
                feature_status, feature_family, feature_metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def update_confidence_for_scan(scan_id: str, symbol: str, confidence: float, risk_grade: str, rank: int | None) -> None:
    """Called after the Confidence Engine runs (which happens after
    capture_snapshot, since confidence needs the full Setup). Cheap
    UPDATE rather than restructuring the capture call site.
    """
    conn = _connect_audit_db()
    try:
        conn.execute(
            "UPDATE indicator_snapshots SET confidence = ?, risk_grade = ?, top15_rank = ? "
            "WHERE scan_id = ? AND symbol = ?",
            (confidence, risk_grade, rank, scan_id, symbol),
        )
        conn.commit()
    finally:
        conn.close()


def backfill_top15_ranks(scan_id: str, rank_by_symbol: dict[str, int]) -> None:
    """Deep Audit patch item #8 fix: `rank` isn't known at the point
    update_confidence_for_scan() runs (Confidence Engine finishes
    before _rank() has assembled the full batch - Document 06 SS8
    pipeline order), so main.py always called it with rank=None
    (confirmed against real data: 04_indicator_snapshot.csv's
    top15_rank column was NULL in all 7413 real rows). This is called
    once, right after main.QDEPipeline._rank() produces the final
    Top15 order, and writes the real rank back for exactly those
    symbols - the same "compute now, correct the already-persisted
    row afterward" pattern already used for uniqueness
    (main.py's _rank() -> repository.update_uniqueness()).

    Only ever narrows existing NULL top15_rank values to a real
    number for genuine Top15 members this scan - never invents ranks
    for symbols that were never in the Top15, and never touches
    historical scans (matches instruction: "Geçmiş kayıtları zorla
    uydurma/backfill etme. Sadece güvenilir şekilde hesaplanabiliyorsa
    backfill yap.").
    """
    if not rank_by_symbol:
        return
    conn = _connect_audit_db()
    try:
        conn.executemany(
            "UPDATE indicator_snapshots SET top15_rank = ? WHERE scan_id = ? AND symbol = ?",
            [(rank, scan_id, symbol) for symbol, rank in rank_by_symbol.items()],
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Missing Data Report (explicit, structured - not hidden in prose)
# ---------------------------------------------------------------------

@dataclass
class MissingMetric:
    name: str
    category: str
    why_it_matters: str
    blocks_analysis: str
    effort: str  # "Low" / "Medium" / "High"
    priority: int  # 1 = add first


MISSING_METRICS: list[MissingMetric] = [
    MissingMetric(
        name="[RESOLVED - see outcome_engine.py] Maximum Favorable/Adverse Excursion (true MFE/MAE)",
        category="Prediction Tracking",
        why_it_matters="Real fixed-horizon MFE/MAE (30m/1h/4h/12h/24h, from historical OHLCV, "
        "never a live price) is now computed by outcome_engine.py and stored in qde.db's "
        "performance_tracking table - this entry is kept only to document that the gap it "
        "describes has been closed, not as an active limitation.",
        blocks_analysis="None - resolved",
        effort="Done",
        priority=2,
    ),
    MissingMetric(
        name="Price-at-scan for rejected/low-score symbols",
        category="False Negative Analysis",
        why_it_matters="Without this, QDE cannot ever prove or disprove whether it is missing "
        "real opportunities by rejecting them too early - the single most important check "
        "for an overly-aggressive Hard Filter or a mis-weighted Scoring Engine.",
        blocks_analysis="False Negative report (retroactively - this audit engine now captures "
        "it going forward via --audit-capture)",
        effort="Low (this module already implements the capture - simply needs weeks of "
        "--audit-capture runs to accumulate)",
        priority=1,
    ),
    MissingMetric(
        name="Raw technical sub-indicators (ATR, ADX, MACD, VWAP, Bollinger bands, candle patterns)",
        category="Indicator Depth",
        why_it_matters="The current Trend/Momentum/Volatility indicators compute simplified "
        "internal features (EMA alignment, RSI, Bollinger band-width percentile) but do not "
        "expose classic named indicators separately. An auditor cannot tell, e.g., whether "
        "ADX-style trend strength would outperform the current EMA-alignment approach without "
        "this data existing to compare against.",
        blocks_analysis="Indicator Snapshot completeness, feature-redundancy analysis "
        "(Document 03 SS11)",
        effort="Medium (each is a new calculation, not just new storage - real engineering "
        "work in indicators/*.py)",
        priority=3,
    ),
    MissingMetric(
        name="Fake Breakout Score / Market Structure classification",
        category="Indicator Depth",
        why_it_matters="Explicitly named as a goal in the project's own founding documents "
        "(Document 01 SS4) but never implemented - QDE currently has no dedicated mechanism "
        "to distinguish a genuine breakout from a fake one beyond the general Confidence "
        "Engine contradiction checks.",
        blocks_analysis="False Positive root-cause analysis (why a high-score signal failed)",
        effort="High (new indicator + validation research before it can be trusted)",
        priority=4,
    ),
    MissingMetric(
        name="Macro market context (BTC Dominance, Fear & Greed, TOTAL2/3, Altseason index)",
        category="Market Regime",
        why_it_matters="Every setup now carries a real market_regime "
        "(bull/bear/sideways/high_volatility, derived from BTC's own trend/volatility "
        "indicators - see market_regime.py) instead of the old constant 'unknown'. What's "
        "still missing is broader macro context beyond BTC alone - without it, QDE cannot "
        "separate 'this indicator combination works' from 'this indicator combination worked "
        "because it was a strong bull market and almost everything worked'.",
        blocks_analysis="Deeper regime-segmented statistics, Learning Engine's "
        "consensus-window logic (which currently approximates regime crudely via aggregate "
        "Top15 return - learning_engine.py's classify_regime())",
        effort="Medium (requires a new external API integration - e.g. CoinGecko for "
        "dominance/TOTAL2-3, alternative.me for Fear & Greed - none currently connected)",
        priority=2,
    ),
    MissingMetric(
        name="Confidence and risk_grade for Hard-Filter-rejected symbols",
        category="Filter Decision Log",
        why_it_matters="Rejected symbols never reach the Confidence Engine (by design - "
        "Document 06 SS3 pipeline order), so it's currently impossible to ask 'would this "
        "rejected symbol have scored well if it had passed the filter?' - useful for judging "
        "whether Hard Filter thresholds are too strict.",
        blocks_analysis="Filter Decision Log depth, Hard Filter threshold tuning",
        effort="Low (would require deliberately running rejected symbols through Scoring/"
        "Confidence anyway for audit purposes only - cheap, but currently not done since "
        "it would waste CPU on symbols already known to fail data-quality gates)",
        priority=5,
    ),
]


def missing_data_report_txt() -> str:
    lines = ["=" * 60, "MISSING DATA REPORT", "=" * 60, "", "Bu rapor QDE'nin su an HENUZ toplamadigi", "ama toplarsa denetimi guclendirecek metrikleri", "onem sirasina gore listeler.", ""]
    for m in sorted(MISSING_METRICS, key=lambda x: x.priority):
        lines.append(f"[Oncelik {m.priority}] {m.name}")
        lines.append(f"  Kategori       : {m.category}")
        lines.append(f"  Neden onemli   : {m.why_it_matters}")
        lines.append(f"  Etkiledigi     : {m.blocks_analysis}")
        lines.append(f"  Tahmini efor   : {m.effort}")
        lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def missing_data_report_json() -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": [
            {
                "priority": m.priority,
                "name": m.name,
                "category": m.category,
                "why_it_matters": m.why_it_matters,
                "blocks_analysis": m.blocks_analysis,
                "effort": m.effort,
            }
            for m in sorted(MISSING_METRICS, key=lambda x: x.priority)
        ],
    }


# ---------------------------------------------------------------------
# Data pulls (all from EXISTING storage - qde.db, performance.db, configs)
# ---------------------------------------------------------------------

def _pull_all_setups() -> list[dict[str, Any]]:
    """Every setup ever recorded (pass or fail) via the existing,
    frozen SetupRepository abstraction - no new interface, no schema
    change to qde.db.
    """
    from storage.sqlite_repository import SQLiteRepository

    repo = SQLiteRepository(db_path=str(_ROOT / "qde.db"))
    try:
        return repo.get_recent_setups(limit=1_000_000)
    finally:
        repo.close()


_REPORTING_HORIZON = "24h"  # primary horizon this report's Top15 history focuses on


def _pull_top15_history(horizon: str = _REPORTING_HORIZON) -> list[dict[str, Any]]:
    """Deep Audit patch item #7 - CRITICAL FIX: this function used to
    call performance_tracker.fetch_current_prices() ONCE per unique
    symbol and apply that single "now" price to every historical row
    for that symbol, regardless of how many weeks old the row's own
    scan was (Deep Audit finding A3 - proven with real production
    data: KAITO showed the exact same current_price across 17 scans
    spanning 17 July - 5 August). That entire mechanism is removed.

    Every row's outcome now comes exclusively from qde.db's
    performance_tracking table, written only by outcome_engine.py from
    real historical OHLCV data anchored to that row's own
    scan_timestamp + horizon - never a live price. A row whose horizon
    hasn't elapsed yet is honestly labeled NOT_COMPLETED; a row whose
    horizon has elapsed but hasn't been processed yet by
    outcome_engine.py is PENDING_PROCESSING; a row outcome_engine.py
    attempted but couldn't find historical data for is MISSING_DATA.
    Nothing here ever fabricates a number for any of these states.
    """
    import performance_tracker as pt

    if not pt._DB_PATH.exists():
        return []
    conn = pt._connect()
    try:
        rows = conn.execute("SELECT * FROM top15_snapshots ORDER BY scan_timestamp").fetchall()
        snapshots = [dict(r) for r in rows]
    finally:
        conn.close()
    if not snapshots:
        return []

    from outcome_engine import HORIZONS

    outcome_by_setup_id: dict[str, dict[str, Any]] = {}
    try:
        from storage.sqlite_repository import SQLiteRepository

        repo = SQLiteRepository(db_path=str(_ROOT / "qde.db"))
        try:
            for s in snapshots:
                sid = s.get("setup_id")
                if sid and sid not in outcome_by_setup_id:
                    outcome = repo.get_outcome(sid, horizon)
                    if outcome is not None:
                        outcome_by_setup_id[sid] = outcome
        finally:
            repo.close()
    except Exception:
        pass  # qde.db unavailable - every row falls back to a honest status below, never a guess

    now_ts = datetime.now(timezone.utc).timestamp()
    horizon_seconds = HORIZONS.get(horizon, 86400)

    for s in snapshots:
        outcome = outcome_by_setup_id.get(s.get("setup_id"))
        s["outcome_horizon"] = horizon
        if outcome is not None and outcome.get("completed"):
            s["outcome_status"] = "COMPLETED"
            s["outcome_target_price"] = outcome.get("target_price")
            s["outcome_return_pct"] = outcome.get("return_pct")
            s["outcome_mfe"] = outcome.get("mfe")
            s["outcome_mae"] = outcome.get("mae")
        elif outcome is not None:
            s["outcome_status"] = "MISSING_DATA"
            s["outcome_target_price"] = s["outcome_return_pct"] = s["outcome_mfe"] = s["outcome_mae"] = None
        else:
            try:
                scan_dt = datetime.fromisoformat(s["scan_timestamp"])
                target_ts = scan_dt.timestamp() + horizon_seconds
                s["outcome_status"] = "NOT_COMPLETED" if target_ts > now_ts else "PENDING_PROCESSING"
            except (ValueError, TypeError):
                s["outcome_status"] = "PENDING_PROCESSING"
            s["outcome_target_price"] = s["outcome_return_pct"] = s["outcome_mfe"] = s["outcome_mae"] = None

    return snapshots


def _pull_audit_snapshots() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not _AUDIT_DB_PATH.exists():
        return [], []
    conn = _connect_audit_db()
    try:
        indicator_rows = [dict(r) for r in conn.execute("SELECT * FROM indicator_snapshots").fetchall()]
        filter_rows = [dict(r) for r in conn.execute("SELECT * FROM filter_decisions").fetchall()]
    finally:
        conn.close()
    return indicator_rows, filter_rows


def _pull_learning_log() -> list[dict[str, Any]]:
    log_path = _ROOT / "learning_log.json"
    if not log_path.exists():
        return []
    try:
        return json.loads(log_path.read_text())
    except json.JSONDecodeError:
        return []


def _pull_configs() -> dict[str, Any]:
    config_dir = _ROOT / "config"
    result = {}
    for name in ("config.json", "filters.json", "weights.json"):
        p = config_dir / name
        if p.exists():
            result[name] = json.loads(p.read_text())
    backup = config_dir / "weights_backup.json"
    if backup.exists():
        result["weights_backup.json"] = json.loads(backup.read_text())
    return result


# ---------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------

def _dicts_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "# no data recorded yet\n"
    buf = io.StringIO()
    fieldnames = sorted({k for row in rows for k in row.keys()})
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def build_false_positive_report(top15_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """High score+confidence signals that ended up losing - built
    exclusively from COMPLETED (real, historical-OHLCV-derived)
    outcomes. A signal whose outcome is NOT_COMPLETED, PENDING_PROCESSING,
    or MISSING_DATA is never included here - "still pending" and "lost"
    must never be conflated (Deep Audit patch item #7).
    """
    out = []
    for s in top15_history:
        if s.get("outcome_status") != "COMPLETED":
            continue
        if s.get("score", 0) >= 80 and s.get("confidence", 0) >= 80 and s["outcome_return_pct"] < 0:
            out.append(
                {
                    "symbol": s["symbol"],
                    "scan_timestamp": s["scan_timestamp"],
                    "score": s["score"],
                    "confidence": s["confidence"],
                    "grade": s.get("grade"),
                    "price_at_scan": s["price_at_scan"],
                    "outcome_horizon": s["outcome_horizon"],
                    "outcome_target_price": s["outcome_target_price"],
                    "outcome_return_pct": s["outcome_return_pct"],
                    "outcome_mfe": s["outcome_mfe"],
                    "outcome_mae": s["outcome_mae"],
                }
            )
    return out


def build_false_negative_report(indicator_rows: list[dict[str, Any]], horizon: str = _REPORTING_HORIZON) -> list[dict[str, Any]]:
    """Rejected/low-score symbols that later moved significantly -
    only possible from real, COMPLETED outcomes (Deep Audit patch item
    #7). audit.db's indicator_snapshots table doesn't store setup_id,
    so each candidate is matched against qde.db's setups table by
    (symbol, scan_timestamp) to find its real setup_id, then that
    setup_id's outcome is looked up - never a live price.
    """
    candidates = [r for r in indicator_rows if not r["passed"] or (r.get("raw_score") or 0) < 50]
    if not candidates:
        return []
    by_symbol: dict[str, dict] = {}
    for r in candidates:
        by_symbol.setdefault(r["symbol"], r)  # first occurrence is fine for a summary pass

    out = []
    try:
        from storage.sqlite_repository import SQLiteRepository

        repo = SQLiteRepository(db_path=str(_ROOT / "qde.db"))
    except Exception:
        return []  # qde.db unavailable - honestly return nothing rather than guess

    try:
        for symbol, row in by_symbol.items():
            db_row = repo._conn.execute(
                "SELECT setup_id FROM setups WHERE symbol = ? AND timestamp = ?",
                (symbol, row.get("scan_timestamp")),
            ).fetchone()
            if db_row is None:
                continue  # no matching setup_id found - skip rather than guess
            setup_id = db_row["setup_id"]
            outcome = repo.get_outcome(setup_id, horizon)
            if outcome is None or not outcome.get("completed"):
                continue  # not COMPLETED yet - never reported as a "missed move" until it's real
            return_pct = outcome.get("return_pct")
            if return_pct is not None and return_pct >= 15:  # meaningfully missed a real move
                out.append(
                    {
                        "symbol": symbol,
                        "scan_timestamp": row["scan_timestamp"],
                        "passed": bool(row["passed"]),
                        "raw_score": row.get("raw_score"),
                        "price_at_scan": row.get("price_at_scan"),
                        "outcome_horizon": horizon,
                        "outcome_target_price": outcome.get("target_price"),
                        "outcome_return_pct": return_pct,
                    }
                )
    finally:
        repo.close()

    return sorted(out, key=lambda r: r["outcome_return_pct"], reverse=True)


def build_statistics(all_setups: list[dict[str, Any]], top15_history: list[dict[str, Any]]) -> dict[str, Any]:
    passed_setups = [s for s in all_setups if s.get("passed")]
    scores = [s["raw_score"] for s in all_setups if s.get("raw_score") is not None]
    confidences = [s["confidence"] for s in passed_setups if s.get("confidence") is not None]

    # Deep Audit patch item #7: only COMPLETED (real, historical-OHLCV-
    # derived) outcomes ever feed these statistics - a NOT_COMPLETED,
    # PENDING_PROCESSING, or MISSING_DATA row contributes nothing here,
    # rather than silently being treated as a 0% or dropped in a way
    # that isn't visible to whoever reads this report.
    completed_rows = [s for s in top15_history if s.get("outcome_status") == "COMPLETED"]
    returns = [s["outcome_return_pct"] for s in completed_rows if s.get("outcome_return_pct") is not None]
    wins = [r for r in returns if r > 0]

    status_counts: dict[str, int] = {}
    for s in top15_history:
        status = s.get("outcome_status", "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1

    def _bucket(values: list[float], edges: list[float]) -> dict[str, int]:
        buckets = {f"{edges[i]}-{edges[i+1]}": 0 for i in range(len(edges) - 1)}
        for v in values:
            for i in range(len(edges) - 1):
                if edges[i] <= v < edges[i + 1]:
                    buckets[f"{edges[i]}-{edges[i+1]}"] += 1
                    break
        return buckets

    return {
        "total_setups_recorded": len(all_setups),
        "total_passed_setups": len(passed_setups),
        "total_top15_entries": len(top15_history),
        "pass_rate_pct": round(len(passed_setups) / len(all_setups) * 100, 2) if all_setups else None,
        "score_distribution": _bucket(scores, [0, 20, 40, 60, 80, 101]),
        "confidence_distribution": _bucket(confidences, [0, 20, 40, 60, 80, 101]),
        "outcome_status_counts": status_counts,
        "reporting_horizon": _REPORTING_HORIZON,
        "completed_outcome_count": len(completed_rows),
        "avg_realized_return_pct": round(statistics.mean(returns), 3) if returns else None,
        "win_rate_pct": round(len(wins) / len(returns) * 100, 2) if returns else None,
        "median_realized_return_pct": round(statistics.median(returns), 3) if returns else None,
    }


def build_ai_prompt() -> str:
    return """Sen bir Quantitative Trading Architect, Machine Learning Engineer ve \
Institutional Trading System Auditor gibi davranacaksin.

Elindeki paket, QDE (Quant Decision Engine) adli bir Binance Futures tarama \
sisteminin tam bir black-box + white-box denetim paketidir. Bu sistem:
  - Binance USDT-M Perpetual Futures'i tarar
  - 7 bagimsiz gostergeden (trend, momentum, volatility, volume, \
relative_strength, open_interest, funding) puan uretir
  - Bir Hard Filter (veri kalitesi), bir Scoring Engine (agirlikli puan) \
ve bir Confidence Engine (gostergeler arasi uyum) katmanindan gecer
  - Bir Learning Engine, istatistiksel olarak anlamli kanit varsa \
agirliklari otomatik ayarlar (guvenlik esikleriyle korumali)

GOREVIN:
1. 02_all_setups.csv, 04_indicator_snapshot.csv ve 07_feature_scores.json \
dosyalarini inceleyerek hangi gostergelerin gercekten sinyal ile getiri \
arasinda korelasyon tasidigini, hangilerinin gurultu oldugunu degerlendir.
2. 06_filter_decisions.csv'yi inceleyerek Hard Filter'in fazla agresif mi \
yoksa fazla gevsek mi calistigini degerlendir.
3. 09_false_positive.csv ve 10_false_negative.csv dosyalarini inceleyerek \
sistemin hangi tur hatalari daha sik yaptigini (yanlis pozitif mi, kacirilan \
firsat mi) tespit et.
4. 11_weight_history.json'i inceleyerek Learning Engine'in yaptigi \
degisikliklerin mantikli olup olmadigini, rollback'lerin dogru tetiklenip \
tetiklenmedigini degerlendir.
5. 13_system_config.json'daki esik degerlerinin (min_quote_volume, \
max_spread, weights) mevcut veriyle tutarli olup olmadigini kontrol et.
6. 16_missing_data_report'u dikkate alarak, hangi eksik verinin \
toplanmasinin analiz kalitesini en cok artiracagini onceliklendir.

CIKTI FORMATI:
- Somut, numaralandirilmis bulgular ver (varsayimla degil, dosyalardaki \
gercek sayilarla destekle).
- Her bulgu icin: sorun / kanit / onerilen aksiyon.
- Mimariyi (Repository pattern, Indicator Registry, UniquenessProvider \
injection) degistirmeyi onerme - bunlar bilinçli olarak dondurulmus \
tasarim kararlaridir; sadece parametreler, esikler ve gosterge agirliklari \
uzerine odaklan.
- Ornek boyutu kucukse (ornegin 20'den az kayit), bunu acikca belirt ve \
sonuclarini "on gozlem" olarak nitelendir, kesin hukum verme.
"""


# ---------------------------------------------------------------------
# Package assembly
# ---------------------------------------------------------------------

def generate_package() -> str:
    """Assembles the full ZIP from whatever real data currently exists.
    Never scans, never mutates any existing file. Returns the output
    zip's filepath.
    """
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    zip_path = _OUTPUT_DIR / f"qde_ai_audit_{timestamp}.zip"

    print("AI Audit paketi olusturuluyor...")
    print("  qde.db okunuyor...")
    all_setups = _pull_all_setups()
    print(f"  {len(all_setups)} setup bulundu.")

    print(f"  Top15 gecmisi okunuyor, gercek outcome'lar ({_REPORTING_HORIZON}) qde.db'den eslestiriliyor...")
    top15_history = _pull_top15_history()
    print(f"  {len(top15_history)} Top15 kaydi islendi.")

    print("  audit.db okunuyor...")
    indicator_rows, filter_rows = _pull_audit_snapshots()
    print(f"  {len(indicator_rows)} gosterge kaydi, {len(filter_rows)} filtre karari bulundu.")

    learning_log = _pull_learning_log()
    configs = _pull_configs()
    false_positives = build_false_positive_report(top15_history)
    false_negatives = build_false_negative_report(indicator_rows)
    stats = build_statistics(all_setups, top15_history)
    print("  Rapor dosyalari hazirlaniyor...")

    summary_lines = [
        "=" * 60,
        "QDE AI AUDIT PACKAGE - SUMMARY",
        "=" * 60,
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"Total setups recorded (qde.db): {len(all_setups)}",
        f"Total Top15 history entries: {len(top15_history)}",
        f"Total audit-capture indicator snapshots: {len(indicator_rows)}",
        f"Total audit-capture filter decisions: {len(filter_rows)}",
        f"Learning Engine log entries: {len(learning_log)}",
        f"False Positive candidates found: {len(false_positives)}",
        f"False Negative candidates found: {len(false_negatives)}",
        "",
        f"Outcome status breakdown ({_REPORTING_HORIZON} horizon): {stats.get('outcome_status_counts')}",
        f"Completed (real, historical) outcomes: {stats.get('completed_outcome_count')}",
        "",
    ]
    pending = stats.get("outcome_status_counts", {}).get("PENDING_PROCESSING", 0)
    if pending:
        summary_lines.append(
            f"NOTE: {pending} signal(s) have an elapsed {_REPORTING_HORIZON} horizon but no "
            f"outcome computed yet. Run 'python outcome_engine.py' to process them with real "
            f"historical data, then regenerate this package."
        )
    if not indicator_rows:
        summary_lines.append(
            "NOTE: No --audit-capture data found yet. 04_indicator_snapshot.csv, "
            "06_filter_decisions.csv, and 10_false_negative.csv will be empty until "
            "at least one scan is run with 'python main.py --audit-capture' or "
            "'python main.py --ai-audit'."
        )
    summary_lines.append("=" * 60)

    files: dict[str, str | bytes] = {
        "01_summary.txt": "\n".join(summary_lines),
        "02_all_setups.csv": _dicts_to_csv(all_setups),
        "03_top_candidates.csv": _dicts_to_csv(top15_history),
        "04_indicator_snapshot.csv": _dicts_to_csv(indicator_rows),
        "05_market_snapshot.json": json.dumps(
            {
                "note": "Macro market context (BTC Dominance, Fear&Greed, TOTAL2/3, Altseason) "
                "is not currently sourced by QDE - see 16_missing_data_report. market_regime "
                "itself IS now real (bull/bear/sideways/high_volatility, derived from BTC's own "
                "trend/volatility indicators - see market_regime.py), not a constant 'unknown'.",
                "distinct_market_regimes_seen": sorted(set(s.get("market_regime", "unknown") for s in all_setups)),
            },
            indent=2,
        ),
        "06_filter_decisions.csv": _dicts_to_csv(filter_rows),
        "07_feature_scores.json": json.dumps(
            [
                {"symbol": s.get("symbol"), "timestamp": s.get("timestamp"), "feature_scores": s.get("feature_scores")}
                for s in all_setups
            ],
            indent=2,
            default=str,
        ),
        "08_prediction_results.csv": _dicts_to_csv(
            [
                {
                    "symbol": s["symbol"],
                    "scan_timestamp": s["scan_timestamp"],
                    "price_at_scan": s["price_at_scan"],
                    "outcome_horizon": s["outcome_horizon"],
                    "outcome_status": s["outcome_status"],
                    "outcome_target_price": s["outcome_target_price"],
                    "outcome_return_pct": s["outcome_return_pct"],
                    "outcome_mfe": s["outcome_mfe"],
                    "outcome_mae": s["outcome_mae"],
                }
                for s in top15_history
            ]
        ),
        "09_false_positive.csv": _dicts_to_csv(false_positives),
        "10_false_negative.csv": _dicts_to_csv(false_negatives),
        "11_weight_history.json": json.dumps(learning_log, indent=2, ensure_ascii=False),
        "12_learning_log.txt": "\n\n".join(json.dumps(e, indent=2, ensure_ascii=False) for e in learning_log) or "# no learning engine runs recorded yet",
        "13_system_config.json": json.dumps(configs, indent=2),
        "14_statistics.json": json.dumps(stats, indent=2, default=str),
        "15_prompt_for_ai.txt": build_ai_prompt(),
        "16_missing_data_report.txt": missing_data_report_txt(),
        "16_missing_data_report.json": json.dumps(missing_data_report_json(), indent=2, ensure_ascii=False),
    }

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)

    return str(zip_path)


def main() -> None:
    path = generate_package()
    print(f"AI Audit package generated: {path}")


if __name__ == "__main__":
    main()
