"""
QDE V1.1 - entry_engine.py

QDE Entry Engine (standalone plugin module).

PURPOSE
    Takes the already-generated Top15 (produced by main.py's existing
    scan/scoring/ranking - never re-scans, never re-ranks, never
    changes Score/Confidence) and re-evaluates it a SECOND time to
    answer a different question: "of these 15 already-good setups,
    which ONE (if any) is genuinely enterable RIGHT NOW?" It then
    computes a technically-grounded Entry Zone, Stop Loss, TP1/TP2/TP3,
    Risk/Reward, and a conservative suggested leverage for that one
    signal - or reports "İŞLEM YOK" if nothing qualifies.

DATA SOURCES (all read-only, zero modification to any existing file)
    1. performance.db's top15_snapshots (most recent scan_id) - the
       exact same Top15 every other plugin (rapor, learning_engine,
       tie_break_optimizer, audit_engine) already reads from. Gives
       symbol, QDE score, confidence, grade, price_at_scan,
       feature_scores (per-indicator WEIGHTED scores - real QDE
       output, not fabricated).
    2. qde.db's setups table (joined via setup_id, via the existing
       frozen SQLiteRepository - read-only usage, same pattern
       audit_engine.py already established) - gives market_regime,
       uniqueness, fingerprint (positive/negative/neutral per
       indicator), and contradictions (Confidence Engine's own
       fake-breakout-adjacent signal - reused here honestly, not
       recomputed).
    3. Fresh OHLCV for ONLY the Top15 symbols (15 fetches, never the
       full 530), via data.exchange.ExchangeClient - imported and used
       exactly as-is, zero modification to that file. Needed because
       genuine breakout/retest/swing-low/resistance/ATR analysis
       requires real candles, which are not persisted anywhere in
       qde.db/performance.db by default.

WHAT THIS MODULE NEVER DOES
    - Never re-scans the 500+ symbol universe.
    - Never changes Setup.raw_score, Result.confidence, ranking, or
      any existing database row.
    - Never proposes SHORT.
    - Never places a real order, tracks a paper position, or monitors
      anything continuously - it runs once, prints/saves a signal (or
      "no signal"), and exits.
    - Never treats Binance order-book depth as real liquidation data
      (the two are explicitly kept conceptually separate - order book
      depth is used, when available, only as a minor confirmation
      signal, never labeled "liquidation").

METHOD - EXPLAINABLE, NOT A BLACK BOX (matches Document 03 SS14's
Explainable AI Principle, the same standard the core QDE engines hold
themselves to)
    Entry Quality (0-100) is a weighted sum of clearly-named,
    independently-inspectable sub-scores - shown in the CLI's
    diagnostic mode, not hidden. No single sub-score can pass a setup
    on its own; a technically ugly setup (chased breakout, no retest,
    contradictions present) is rejected even if QDE's own Score/
    Confidence were high, because Entry Engine is answering a
    genuinely different question than the core engines already did.

Usage:
    python entry_engine.py              # evaluate latest Top15, print + save signal
    python entry_engine.py --explain    # also print the full sub-score breakdown
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_SIGNAL_DB_PATH = _ROOT / "entry_signals.db"

# --- Tunable, explicit constants (never hidden magic numbers) ---
ENTRY_QUALITY_THRESHOLD = 60.0
SWING_LOOKBACK = 20  # candles used to find the recent swing low/high
BREAKOUT_LOOKBACK = 15  # reference-peak lookback: how far back the structural high is computed from
# Breakout Detector V2 (approved architecture - these are explicitly
# NOT the same concept and must never be conflated):
#   - BREAKOUT_SEARCH_WINDOW: how far back the breakout EVENT itself
#     may have occurred (was called `search_window`, defaulted to 6 -
#     found, via 3 real production scans, to be far too narrow: a
#     breakout that happened 7+ candles ago and is now in a clean
#     retest/hold phase was structurally invisible to the detector).
#   - RETEST_VALIDITY_WINDOW: once a breakout IS found, how many
#     candles AFTER it a retest must occur within to still count - a
#     separate concept from the search window above. Not yet
#     calibrated against real outcome data; an explicit, isolated
#     starting configuration per the approved architecture, not a
#     claimed-optimal value.
BREAKOUT_SEARCH_WINDOW = 16
RETEST_VALIDITY_WINDOW = 6
ATR_PERIOD = 14
MAX_EXTENSION_ATR_MULTIPLES = 2.0  # beyond this, price has "chased" too far past breakout
MAX_STOP_DISTANCE_PCT = 12.0  # reject if SL would require more than this % risk

# Rejim bazli ATR carpanlari - stop loss mesafesini piyasa kosullarina gore ayarlar
REGIME_ATR_MULTIPLIERS = {
    "bull": 1.5,
    "bear": 2.5,
    "sideways": 2.0,
    "high_volatility": 3.0,
    "unknown": 2.0,
}
ASSUMED_RISK_PER_TRADE_PCT = 1.5  # conservative account-risk assumption, used only for leverage sizing
MAX_SUGGESTED_LEVERAGE = 10.0
MIN_RR_TP1 = 1.2  # reject if even TP1 doesn't clear this risk/reward

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entry_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    qde_score REAL,
    confidence REAL,
    entry_quality REAL,
    entry_zone_low REAL,
    entry_zone_high REAL,
    reference_entry REAL,
    stop_loss REAL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    risk_label TEXT,
    rr_tp1 REAL,
    rr_tp2 REAL,
    rr_tp3 REAL,
    suggested_leverage REAL,
    breakout_confirmed INTEGER,
    retest_confirmed INTEGER,
    volume_confirmed INTEGER,
    higher_tf_aligned INTEGER,
    signal_status TEXT,
    breakdown_json TEXT
);

-- Every candidate Entry Engine evaluated on every run - not just the one
-- it selected. Logging only: nothing here feeds back into scoring.
-- One row per (evaluation run, symbol). Re-running `entry` on the same
-- scan writes a fresh set (live prices differ run to run) - those are
-- separate observations; de-duplicate at analysis time (e.g. keep the
-- first run_timestamp per scan_id+symbol).
CREATE TABLE IF NOT EXISTS entry_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    setup_id TEXT,
    symbol TEXT NOT NULL,
    rank INTEGER,
    qde_score REAL,
    qde_confidence REAL,
    price_at_scan REAL,
    live_price REAL,
    selected INTEGER NOT NULL DEFAULT 0,
    entry_quality REAL,
    -- raw technical context (needed to re-fit threshold bands later)
    breakout_confirmed INTEGER,
    retest_confirmed INTEGER,
    breakout_level REAL,
    atr REAL,
    extension_atr REAL,
    breakout_candles_ago INTEGER,
    swing_low REAL,
    candles_1h INTEGER,
    -- structural health (365D)
    structural_status TEXT,
    structural_ath REAL,
    structural_ath_distance_pct REAL,
    structural_penalty REAL,
    -- HTF alignment V2 (component scores, not just labels)
    htf_status TEXT,
    htf_trend_1h_score REAL,
    htf_structure_1h_score REAL,
    htf_context_4h_score REAL,
    htf_points_earned REAL,
    htf_points_possible REAL,
    breakdown_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_entry_candidates_scan ON entry_candidates (scan_id, symbol);
CREATE INDEX IF NOT EXISTS idx_entry_candidates_setup ON entry_candidates (setup_id);
"""


def _connect_signal_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_SIGNAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------

@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Top15Candidate:
    """One Top15 symbol enriched with everything already persisted for
    it - nothing here is recomputed, all sourced from performance.db /
    qde.db as-is.
    """
    symbol: str
    scan_id: str
    rank: int
    qde_score: float
    confidence: float
    grade: str
    price_at_scan: float | None
    feature_scores: dict[str, float] = field(default_factory=dict)
    market_regime: str | None = None
    uniqueness: float | None = None
    fingerprint: dict[str, str] = field(default_factory=dict)
    contradictions: list[str] = field(default_factory=list)
    # Links this candidate to qde.db's setups / performance_tracking rows
    # (outcome_engine.py keys its fixed-horizon outcomes by setup_id) so
    # logged Entry Quality breakdowns can later be joined to what the
    # price actually did. NULL for rows from before this column existed.
    setup_id: str | None = None


@dataclass
class EntryQualityBreakdown:
    breakout_score: float = 0.0
    retest_score: float = 0.0
    extension_penalty: float = 0.0
    volume_score: float = 0.0
    trend_momentum_score: float = 0.0
    higher_tf_score: float = 0.0
    contradiction_penalty: float = 0.0
    liquidity_bonus: float = 0.0
    # Structural Health (365D ATH distance) - penalty-only, no hard
    # reject, per the approved specification. status/ath/distance are
    # carried here (not just the penalty) so formatter.py-equivalent
    # output code never has to recompute anything itself.
    structural_penalty: float = 0.0
    structural_status: str = "VERI_YETERSIZ"
    structural_ath: float | None = None
    structural_ath_distance_pct: float | None = None
    # HTF Alignment V2 - real, coin-specific 1H/4H structure (replaces
    # the old market_regime proxy). higher_tf_score above IS
    # points_earned (un-rescaled, per approved spec - a 5/7 PARTIAL
    # result contributes exactly 5 points, never inflated to "5/7*10").
    htf_trend_1h_label: str = "INSUFFICIENT_DATA"
    htf_structure_1h_label: str = "INSUFFICIENT_DATA"
    htf_context_4h_label: str = "INSUFFICIENT_DATA"
    htf_points_possible: float = 0.0
    htf_status: str = "INSUFFICIENT_DATA"

    @property
    def total(self) -> float:
        raw = (
            self.breakout_score
            + self.retest_score
            + self.volume_score
            + self.trend_momentum_score
            + self.higher_tf_score
            + self.liquidity_bonus
            - self.extension_penalty
            - self.contradiction_penalty
            - self.structural_penalty
        )
        return round(max(0.0, min(100.0, raw)), 1)

    def as_dict(self) -> dict[str, float]:
        return {
            "breakout": round(self.breakout_score, 1),
            "retest": round(self.retest_score, 1),
            "extension_penalty": round(-self.extension_penalty, 1),
            "volume": round(self.volume_score, 1),
            "trend_momentum": round(self.trend_momentum_score, 1),
            "higher_timeframe": round(self.higher_tf_score, 1),
            "contradiction_penalty": round(-self.contradiction_penalty, 1),
            "liquidity_bonus": round(self.liquidity_bonus, 1),
            "structural_penalty": round(-self.structural_penalty, 1),
            "structural_status": self.structural_status,
            "structural_ath": self.structural_ath,
            "structural_ath_distance_pct": self.structural_ath_distance_pct,
            "htf_trend_1h": self.htf_trend_1h_label,
            "htf_structure_1h": self.htf_structure_1h_label,
            "htf_context_4h": self.htf_context_4h_label,
            "htf_points": f"{self.higher_tf_score:.1f}/{self.htf_points_possible:.1f}",
            "htf_status": self.htf_status,
            "total": self.total,
        }


@dataclass
class TechnicalContext:
    """Everything derived from fresh OHLCV for one symbol - pure,
    testable, no I/O inside these computations themselves.
    """
    current_price: float
    swing_low: float | None
    recent_high: float | None  # the level a breakout would need to clear
    atr: float | None
    breakout_confirmed: bool
    retest_confirmed: bool
    extension_atr_multiples: float | None
    candles_available: int
    # How many candles before the latest one the detected breakout closed.
    # Logged so extension penalties can be explained: a breakout found
    # 14-16 candles back in a steadily trending coin leaves a stale
    # reference level far below price (see detect_breakout's note).
    breakout_candles_ago: int | None = None


@dataclass
class EntrySignal:
    symbol: str
    scan_id: str
    direction: str  # always "LONG" in V1
    qde_score: float
    confidence: float
    entry_quality: float
    entry_zone: tuple[float, float]
    reference_entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_label: str
    rr: dict[str, float]
    suggested_leverage: float
    breakout_confirmed: bool
    retest_confirmed: bool
    volume_confirmed: bool
    higher_tf_aligned: bool
    breakdown: EntryQualityBreakdown
    status: str = "GİRİLEBİLİR"


# ---------------------------------------------------------------------
# Stage 1: Latest Top15 retrieval (performance.db + qde.db, read-only)
# ---------------------------------------------------------------------

def fetch_latest_top15() -> list[Top15Candidate]:
    import performance_tracker as pt

    if not pt._DB_PATH.exists():
        return []
    conn = pt._connect()
    try:
        latest_scan_id_row = conn.execute(
            "SELECT scan_id FROM top15_snapshots ORDER BY scan_timestamp DESC LIMIT 1"
        ).fetchone()
        if latest_scan_id_row is None:
            return []
        scan_id = latest_scan_id_row["scan_id"]
        rows = conn.execute(
            "SELECT * FROM top15_snapshots WHERE scan_id = ? ORDER BY rank ASC",
            (scan_id,),
        ).fetchall()
    finally:
        conn.close()

    candidates = [
        Top15Candidate(
            symbol=r["symbol"],
            scan_id=r["scan_id"],
            rank=r["rank"],
            qde_score=r["score"],
            confidence=r["confidence"],
            grade=r["grade"],
            price_at_scan=r["price_at_scan"],
            feature_scores=json.loads(r["feature_scores_json"] or "{}"),
            setup_id=r["setup_id"] if "setup_id" in r.keys() else None,
        )
        for r in rows
    ]

    _enrich_from_qde_db(candidates)
    return candidates


def _enrich_from_qde_db(candidates: list[Top15Candidate]) -> None:
    """Joins in market_regime/uniqueness/fingerprint/contradictions
    from qde.db, via the existing frozen SQLiteRepository - read-only,
    best-effort (if qde.db is missing or a symbol isn't found, the
    candidate simply keeps its defaults - Entry Quality degrades
    gracefully rather than crashing, per explicit instruction).
    """
    try:
        from storage.sqlite_repository import SQLiteRepository

        repo = SQLiteRepository(db_path=str(_ROOT / "qde.db"))
        try:
            recent = repo.get_recent_setups(limit=5000)
        finally:
            repo.close()
    except Exception:
        return

    by_symbol_latest: dict[str, dict[str, Any]] = {}
    for row in recent:
        sym = row["symbol"]
        if sym not in by_symbol_latest or row["timestamp"] > by_symbol_latest[sym]["timestamp"]:
            by_symbol_latest[sym] = row

    for c in candidates:
        row = by_symbol_latest.get(c.symbol)
        if row is None:
            continue
        c.market_regime = row.get("market_regime")
        c.uniqueness = row.get("uniqueness")
        c.fingerprint = row.get("fingerprint") or {}
        c.contradictions = row.get("contradictions") or []


# ---------------------------------------------------------------------
# Stage 2: Fresh OHLCV for Top15 symbols only (reuses ExchangeClient as-is)
# ---------------------------------------------------------------------

async def fetch_top15_candles(symbols: list[str], timeframe: str = "1h", limit: int = 60) -> dict[str, list[Candle]]:
    """Fetches OHLCV for exactly these symbols - never a fresh
    530-symbol scan. Reuses data.exchange.ExchangeClient completely
    as-is (imported, not modified) with a standalone rate-limit config
    matching the pattern already established in performance_tracker.py
    /audit_engine.py/learning_engine.py's own short-lived exchange
    clients.

    LOOK-AHEAD FIX (Structural Health revision): previously called
    ExchangeClient.get_ohlcv() directly, bypassing the look-ahead
    protection that data/collector.py's DataCollector applies to the
    core QDE scan (dropping Binance's still-forming last candle - see
    data/collector.py's _drop_unclosed_candle and its own module
    docstring). This meant Entry Engine's breakout/retest detection
    could use an unclosed, still-moving candle's close as if it were
    final. Fixed by reusing _drop_unclosed_candle here (pure import,
    zero modification to data/collector.py) - the only candle-fetching
    path in this file, confirmed via source inspection that no
    separate 15m path exists, so this one fix closes the gap
    completely for Entry Engine.
    """
    from data.exchange import ExchangeClient
    from data.collector import _drop_unclosed_candle

    exchange = ExchangeClient(rate_limit_config={"requests_per_second": 8.0, "max_retries": 3, "burst": 50})
    result: dict[str, list[Candle]] = {}
    try:
        for symbol in symbols:
            try:
                # +1 to compensate for a possible drop, same reasoning
                # as DataCollector._fetch_timeframe's own look-ahead fix.
                raw = await exchange.get_ohlcv(symbol, timeframe, limit + 1)
                candles = [
                    Candle(timestamp=int(row[0]), open=row[1], high=row[2], low=row[3], close=row[4], volume=row[5])
                    for row in raw
                ]
                result[symbol] = _drop_unclosed_candle(candles, timeframe)
            except Exception:
                result[symbol] = []
    finally:
        await exchange.close()
    return result


# ---------------------------------------------------------------------
# Stage 3: Pure technical analysis (testable without any network)
# ---------------------------------------------------------------------

def compute_atr(candles: list[Candle], period: int = ATR_PERIOD) -> float | None:
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i].high, candles[i].low, candles[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    return sum(true_ranges[-period:]) / period


def find_swing_low(candles: list[Candle], lookback: int = SWING_LOOKBACK) -> float | None:
    if not candles:
        return None
    window = candles[-lookback:] if len(candles) >= lookback else candles
    return min(c.low for c in window)


def find_recent_high(candles: list[Candle], lookback: int = BREAKOUT_LOOKBACK, end_index: int | None = None) -> float | None:
    """The structural level a breakout needs to have cleared - the
    highest high in the `lookback`-candle window ending strictly
    before `end_index` (or before the last candle, if end_index is
    None). Used both to detect a fresh breakout and, when scanning
    backward for one, to test each candidate breakout candle in turn.
    """
    if end_index is None:
        end_index = len(candles) - 1
    start = end_index - lookback
    if start < 0:
        return None
    window = candles[start:end_index]
    if not window:
        return None
    return max(c.high for c in window)


def detect_breakout(candles: list[Candle], search_window: int = BREAKOUT_SEARCH_WINDOW) -> tuple[bool, float | None, int | None]:
    """Searches the last `search_window` candles (not just the very
    last one) for the EARLIEST candle whose CLOSE broke above the
    structural high computed from candles strictly before it - this
    matters because the real KIRILIM -> RETEST -> ONAY flow means the
    breakout itself may have happened several candles ago, with more
    recent candles representing the retest/hold phase, not the
    breakout moment itself (a real bug caught by testing a genuine
    breakout+retest sequence - the original single-last-candle check
    incorrectly folded the breakout candle's own high into its own
    "prior structure" level).

    `search_window` is ONLY how far back the breakout EVENT may have
    occurred - it has nothing to do with how long a retest stays valid
    afterward (see RETEST_VALIDITY_WINDOW / detect_retest - Breakout
    Detector V2's explicit separation of these two concepts).

    Returns (confirmed, breakout_level, breakout_candle_index).

    KNOWN BEHAVIOR (documented, deliberately NOT changed here): when the
    window holds several close-above-structure candles - e.g. a coin in
    a steady uptrend printing a new 15-candle high every few candles -
    the EARLIEST one is returned, so the reference level can be up to
    `search_window` candles stale and extension (measured from that
    level) grows with the window. With the old 6-candle window this was
    bounded; with 16 it is not. Whether this explains the -40 extension
    penalties seen on real scans is an open question that
    entry_candidates.breakout_candles_ago exists to answer; any change to
    the selection rule should be made against that data, not by guess.
    """
    if len(candles) < BREAKOUT_LOOKBACK + 2:
        return False, None, None

    # -1: a breakout exactly `search_window` candles before the last
    # one must be INCLUDED (the boundary case), not excluded - found
    # via an off-by-one while building the exact "16 mum önce dahil,
    # 17 mum önce hariç" boundary test the user specified.
    search_start = max(BREAKOUT_LOOKBACK, len(candles) - 1 - search_window)
    for i in range(search_start, len(candles)):
        level = find_recent_high(candles, lookback=BREAKOUT_LOOKBACK, end_index=i)
        if level is not None and candles[i].close > level:
            return True, level, i
    return False, None, None


def detect_retest(
    candles: list[Candle],
    breakout_level: float,
    breakout_index: int,
    retest_window: int = RETEST_VALIDITY_WINDOW,
    tolerance_pct: float = 2.5,
) -> bool:
    """After the breakout candle (at `breakout_index`), did price come
    back down near the breakout level (within `tolerance_pct`) at any
    point AND hold above it since (no close back below the level)?

    `retest_window` (Breakout Detector V2 - explicitly separate from
    how far back the breakout itself may have occurred, see
    BREAKOUT_SEARCH_WINDOW / detect_breakout): only the `retest_window`
    candles immediately after the breakout are considered - a retest
    that would only show up later than that is NOT counted as valid.
    This bounds "retest geçerliliği" to a fixed, inspectable window
    instead of silently extending all the way to "now" (which used to
    mean a breakout found near the edge of a wide search window could
    have an arbitrarily long, uncapped retest-search tail).
    """
    post_breakout = candles[breakout_index + 1: breakout_index + 1 + retest_window]
    if breakout_level <= 0 or not post_breakout:
        return False
    came_near = any(
        abs(c.low - breakout_level) / breakout_level * 100 <= tolerance_pct for c in post_breakout
    )
    held_above = all(c.close >= breakout_level * 0.995 for c in post_breakout)
    return came_near and held_above


def compute_extension(current_price: float, breakout_level: float, atr: float | None) -> float | None:
    """How many ATRs the current price has moved past the breakout
    level - the "did we chase it" measure (item #3 in the spec).
    """
    if atr is None or atr <= 0 or breakout_level <= 0:
        return None
    return (current_price - breakout_level) / atr


def analyze_technicals(candles: list[Candle]) -> TechnicalContext | None:
    if not candles:
        return None
    current_price = candles[-1].close
    swing_low = find_swing_low(candles)
    breakout_confirmed, recent_high, breakout_index = detect_breakout(candles)
    atr = compute_atr(candles)
    retest_confirmed = (
        detect_retest(candles, recent_high, breakout_index)
        if (breakout_confirmed and recent_high is not None and breakout_index is not None)
        else False
    )
    extension = compute_extension(current_price, recent_high, atr) if recent_high else None

    return TechnicalContext(
        current_price=current_price,
        swing_low=swing_low,
        recent_high=recent_high,
        atr=atr,
        breakout_confirmed=breakout_confirmed,
        retest_confirmed=retest_confirmed,
        extension_atr_multiples=extension,
        candles_available=len(candles),
        breakout_candles_ago=(len(candles) - 1 - breakout_index) if (breakout_confirmed and breakout_index is not None) else None,
    )


# ---------------------------------------------------------------------
# Stage 3b: Structural Health (365D ATH distance - separate, testable
# component per the approved specification; never folded directly into
# score_entry_quality's body).
# ---------------------------------------------------------------------

STRUCTURAL_HEALTH_MIN_DAYS = 365
_STRUCTURAL_PENALTY_BANDS: list[tuple[float, float, float, float]] = [
    # (band_start_pct, band_end_pct, penalty_at_start, penalty_at_end) - all as
    # positive distances below ATH (i.e. 50 means "-50%"). Exactly the
    # table confirmed with the user: 0-50% -> 0, 50-80% -> 0..-10,
    # 80-90% -> -10..-20, 90-95% -> -20..-30, 95-100% -> -30..-40 (cap).
    (0.0, 50.0, 0.0, 0.0),
    (50.0, 80.0, 0.0, 10.0),
    (80.0, 90.0, 10.0, 20.0),
    (90.0, 95.0, 20.0, 30.0),
    (95.0, 100.0, 30.0, 40.0),
]


def _structural_penalty_curve(distance_pct: float) -> float:
    """Pure function: distance_pct is the NEGATIVE distance from ATH
    expressed as a positive number (e.g. 98.3 for "-98.3% from ATH").
    Returns the graduated penalty (positive number, to be SUBTRACTED
    from Entry Quality), per the exact band table above. Linear
    interpolation within each band; capped at 40.0 beyond -100%
    (defensive only - price distance cannot exceed 100% in practice).
    """
    distance_pct = max(0.0, distance_pct)
    if distance_pct >= 100.0:
        return 40.0
    for band_start, band_end, penalty_start, penalty_end in _STRUCTURAL_PENALTY_BANDS:
        if band_start <= distance_pct <= band_end:
            if band_end == band_start:
                return penalty_start
            fraction = (distance_pct - band_start) / (band_end - band_start)
            return round(penalty_start + fraction * (penalty_end - penalty_start), 2)
    return 40.0  # unreachable given the bands above cover [0, 100], kept as a safe fallback


@dataclass
class StructuralHealthResult:
    """Carries both the penalty AND the underlying numbers (ATH,
    distance, status label) so the output/formatting layer never has
    to recompute anything itself - per the approved specification.
    """
    status: str  # "SAGLIKLI" | "DIKKAT" | "YUKSEK_RISK" | "COK_YUKSEK_RISK" | "VERI_YETERSIZ"
    ath: float | None
    ath_distance_pct: float | None  # signed: negative = below ATH
    penalty: float
    candles_available: int


def _structural_status_label(distance_pct: float) -> str:
    magnitude = abs(distance_pct)
    if magnitude < 50.0:
        return "SAGLIKLI"
    if magnitude < 80.0:
        return "DIKKAT"
    if magnitude < 95.0:
        return "YUKSEK_RISK"
    return "COK_YUKSEK_RISK"


def analyze_structural_health(
    daily_candles: list[Candle],
    current_price: float,
    min_days: int = STRUCTURAL_HEALTH_MIN_DAYS,
) -> StructuralHealthResult:
    """Pure function - takes ALREADY-fetched, ALREADY-look-ahead-safe
    daily candles (the caller is responsible for passing candles that
    have already gone through _drop_unclosed_candle, exactly like
    fetch_top15_candles already does for every timeframe it fetches)
    and a live current price.

    ATH is the maximum `high` across every CLOSED daily candle given -
    never today's still-forming candle, never a live intraday high.
    distance_pct compares the live current_price against that
    closed-candle ATH, which is intentional: "how far below the
    all-time high are we RIGHT NOW" needs a live numerator, but the
    ATH denominator itself must never be influenced by an unclosed
    candle (look-ahead safety - confirmed by test_outcome_engine.py
    -style regression test, see tests/test_entry_engine.py).

    No hard reject: insufficient history (< min_days candles) returns
    penalty=0.0, status="VERI_YETERSIZ" - never a guessed ATH, never a
    fabricated penalty. A severe drawdown never returns more than the
    40.0-point cap, by construction of _structural_penalty_curve.
    """
    if not daily_candles or len(daily_candles) < min_days or current_price <= 0:
        return StructuralHealthResult(
            status="VERI_YETERSIZ", ath=None, ath_distance_pct=None, penalty=0.0,
            candles_available=len(daily_candles),
        )

    ath = max(c.high for c in daily_candles)
    if ath <= 0:
        return StructuralHealthResult(
            status="VERI_YETERSIZ", ath=None, ath_distance_pct=None, penalty=0.0,
            candles_available=len(daily_candles),
        )

    distance_pct = (current_price - ath) / ath * 100.0  # negative, e.g. -98.3
    penalty = _structural_penalty_curve(abs(distance_pct))
    status = _structural_status_label(distance_pct)

    return StructuralHealthResult(
        status=status,
        ath=round(ath, 10),
        ath_distance_pct=round(distance_pct, 2),
        penalty=penalty,
        candles_available=len(daily_candles),
    )


# ---------------------------------------------------------------------
# Stage 3c: HTF Alignment V2 (real, coin-specific - replaces the old
# market_regime proxy, per approved architecture)
# ---------------------------------------------------------------------

HTF_TREND_1H_MAX = 4.0
HTF_STRUCTURE_1H_MAX = 3.0
HTF_CONTEXT_4H_MAX = 3.0
HTF_EMA_FAST_PERIOD = 20
HTF_EMA_SLOW_PERIOD = 50
# How far apart (in %) EMA20/EMA50 must be before being called a clear
# BULLISH/BEARISH relationship rather than NEUTRAL - prevents a
# marginal, noise-level EMA ordering from being read as a strong
# signal (approved requirement: "EMA'lar arasindaki fark cok kucukse
# bunu guclu trend olarak yorumlamayalim").
HTF_EMA_STRONG_SEPARATION_PCT = 2.0
HTF_STRUCTURE_LOOKBACK = 20  # candles used for the recent realized range


def compute_ema(closes: list[float], period: int) -> float | None:
    """Standard EMA, SMA-seeded. Returns None (not 0.0 or a guess) if
    there isn't enough closed-candle history - callers must treat None
    as "not computed", never as a bearish/neutral reading.
    """
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def _ema_relationship(closes: list[float]) -> tuple[float | None, float | None, str]:
    """Returns (ema_fast, ema_slow, label). label is BULLISH/BEARISH/
    NEUTRAL/INSUFFICIENT_DATA - EMA ranking only (fast above/below/near
    slow), explicitly NOT a crossover-event detector (approved
    requirement: "EMA20nin EMA50 uzerinde olmasi, mutlaka yeni bir
    bullish kesisim gerceklestigi anlamina gelmez" - true crossover
    detection is an explicitly deferred, separate future feature).
    """
    ema_fast = compute_ema(closes, HTF_EMA_FAST_PERIOD)
    ema_slow = compute_ema(closes, HTF_EMA_SLOW_PERIOD)
    if ema_fast is None or ema_slow is None or ema_slow == 0:
        return ema_fast, ema_slow, "INSUFFICIENT_DATA"
    diff_pct = (ema_fast - ema_slow) / ema_slow * 100
    if diff_pct >= HTF_EMA_STRONG_SEPARATION_PCT * 0.25:  # a quarter of the "strong" threshold already counts as a real (if mild) lean
        label = "BULLISH"
    elif diff_pct <= -HTF_EMA_STRONG_SEPARATION_PCT * 0.25:
        label = "BEARISH"
    else:
        label = "NEUTRAL"
    return ema_fast, ema_slow, label


def _ema_relationship_score(closes: list[float], max_points: float) -> tuple[float | None, str]:
    """Smooth, monotonic, symmetric score: 0.5*max_points at diff=0%
    (truly neutral), scaling linearly up to max_points at
    +HTF_EMA_STRONG_SEPARATION_PCT (clearly bullish) and down to 0.0 at
    -HTF_EMA_STRONG_SEPARATION_PCT (clearly bearish), clamped beyond
    those bounds. A near-zero EMA gap naturally lands near the
    midpoint - no separate deadzone special-case needed for the score
    itself (the _ema_relationship label above is the human-readable
    categorization, kept deliberately separate from this smooth score).

    Returns (score, label). score is None if data was insufficient -
    callers must not treat None as 0.0 (that would silently read as
    "maximally bearish" rather than "not computed").
    """
    ema_fast, ema_slow, label = _ema_relationship(closes)
    if label == "INSUFFICIENT_DATA":
        return None, label
    diff_pct = (ema_fast - ema_slow) / ema_slow * 100
    fraction = 0.5 + diff_pct / (2 * HTF_EMA_STRONG_SEPARATION_PCT)
    fraction = max(0.0, min(1.0, fraction))
    return round(max_points * fraction, 2), label


def _structure_score(candles: list[Candle], current_price: float, max_points: float) -> tuple[float | None, str]:
    """Where does current_price sit within its own recent realized
    range (last HTF_STRUCTURE_LOOKBACK closed candles)? 0 = at the
    range low, 1 = at the range high, linear in between.

    HONEST V1 LIMITATION (approved, documented rather than guessed):
    this does NOT attempt to detect swing-high/swing-low DIRECTION
    (i.e. whether recent swing lows are rising or falling) - that
    would require reliable pivot detection this version doesn't
    implement. It also does not yet discount a reading taken inside an
    unusually tight range. Both are explicitly left as future, separate
    refinements rather than being faked here.
    """
    if len(candles) < HTF_STRUCTURE_LOOKBACK:
        return None, "INSUFFICIENT_DATA"
    window = candles[-HTF_STRUCTURE_LOOKBACK:]
    range_high = max(c.high for c in window)
    range_low = min(c.low for c in window)
    if range_high <= range_low:
        return max_points * 0.5, "NEUTRAL"
    position = (current_price - range_low) / (range_high - range_low)
    position = max(0.0, min(1.0, position))
    label = "BULLISH" if position >= 0.6 else "BEARISH" if position <= 0.4 else "NEUTRAL"
    return round(max_points * position, 2), label


@dataclass
class HTFAlignmentResult:
    """Carries score, status, AND reason separately for every
    component - per the approved requirement that insufficient data
    must never be silently read as a bearish signal, and that a
    partial result (e.g. 4H missing) must show what fraction of the
    TOTAL POSSIBLE points it represents, never rescaled to hide the gap.
    """
    trend_1h_score: float | None
    trend_1h_label: str
    structure_1h_score: float | None
    structure_1h_label: str
    context_4h_score: float | None
    context_4h_label: str
    points_earned: float
    points_possible: float
    status: str  # "FULL" | "PARTIAL" | "INSUFFICIENT_DATA"


def analyze_htf_alignment(
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    current_price: float,
) -> HTFAlignmentResult:
    """Pure function - takes ALREADY-fetched, ALREADY-look-ahead-safe
    1H and 4H candles (both must have already gone through
    fetch_top15_candles's _drop_unclosed_candle, same as every other
    timeframe this module uses) and computes three independent
    components. market_regime is NEVER consulted here - this is
    entirely coin-specific, per the approved architecture
    ("market_regime bu puanin hesabina girmeyecek").
    """
    trend_score, trend_label = _ema_relationship_score([c.close for c in candles_1h], HTF_TREND_1H_MAX)
    structure_score, structure_label = _structure_score(candles_1h, current_price, HTF_STRUCTURE_1H_MAX)
    context_score, context_label = _ema_relationship_score([c.close for c in candles_4h], HTF_CONTEXT_4H_MAX)

    components = [
        (trend_score, HTF_TREND_1H_MAX),
        (structure_score, HTF_STRUCTURE_1H_MAX),
        (context_score, HTF_CONTEXT_4H_MAX),
    ]
    points_earned = sum(s for s, _ in components if s is not None)
    points_possible = sum(m for s, m in components if s is not None)
    computed_count = sum(1 for s, _ in components if s is not None)

    if computed_count == 0:
        status = "INSUFFICIENT_DATA"
    elif computed_count == len(components):
        status = "FULL"
    else:
        status = "PARTIAL"

    return HTFAlignmentResult(
        trend_1h_score=trend_score,
        trend_1h_label=trend_label,
        structure_1h_score=structure_score,
        structure_1h_label=structure_label,
        context_4h_score=context_score,
        context_4h_label=context_label,
        points_earned=round(points_earned, 2),
        points_possible=round(points_possible, 2),
        status=status,
    )


# ---------------------------------------------------------------------
# Stage 4: Entry Quality scoring (explainable, weighted, capped)
# ---------------------------------------------------------------------

def score_entry_quality(
    candidate: Top15Candidate,
    tech: TechnicalContext | None,
    structural: "StructuralHealthResult | None" = None,
    htf: "HTFAlignmentResult | None" = None,
) -> EntryQualityBreakdown:
    b = EntryQualityBreakdown()

    # Structural Health is independent of the 1h technical analysis
    # below (different data source - daily candles), so it's applied
    # unconditionally here, even if `tech` ends up None/insufficient.
    if structural is not None:
        b.structural_penalty = structural.penalty
        b.structural_status = structural.status
        b.structural_ath = structural.ath
        b.structural_ath_distance_pct = structural.ath_distance_pct

    # HTF Alignment V2 - same independence: applied regardless of
    # whether the 1h breakout/retest check below finds anything,
    # since it's a real, coin-specific structural reading, not
    # contingent on a breakout having happened.
    if htf is not None:
        b.higher_tf_score = htf.points_earned
        b.htf_trend_1h_label = htf.trend_1h_label
        b.htf_structure_1h_label = htf.structure_1h_label
        b.htf_context_4h_label = htf.context_4h_label
        b.htf_points_possible = htf.points_possible
        b.htf_status = htf.status

    if tech is None or tech.candles_available < BREAKOUT_LOOKBACK + 1:
        # Not enough fresh data - honestly reflected as zero technical
        # score rather than guessing (Document 03 SS13 pattern reused).
        return b

    # Breakout Quality (0-25): confirmed close-above-structure breakout.
    if tech.breakout_confirmed:
        b.breakout_score = 25.0

    # Retest Quality (0-20): retested and held is safer than a raw,
    # un-retested breakout - rewarded accordingly, but a fresh breakout
    # without a retest is not disqualified outright (item #3 prefers
    # retest, doesn't mandate it exclusively).
    if tech.retest_confirmed:
        b.retest_score = 20.0
    elif tech.breakout_confirmed:
        b.retest_score = 8.0  # fresh, unconfirmed-by-retest breakout: partial credit only

    # Extension Penalty: the core "don't chase it" rule (item #3).
    if tech.extension_atr_multiples is not None and tech.extension_atr_multiples > 0:
        if tech.extension_atr_multiples > MAX_EXTENSION_ATR_MULTIPLES:
            b.extension_penalty = 40.0  # heavy, effectively disqualifying on its own
        elif tech.extension_atr_multiples > MAX_EXTENSION_ATR_MULTIPLES * 0.5:
            b.extension_penalty = 15.0

    # Volume Confirmation (0-15): reuses QDE's OWN already-computed
    # weighted volume score - real data, not recomputed.
    volume_feature = candidate.feature_scores.get("volume", 0.0)
    b.volume_score = max(0.0, min(15.0, volume_feature))

    # Trend/Momentum Health (0-20): same principle, reusing QDE's own
    # weighted trend+momentum scores.
    trend_feature = candidate.feature_scores.get("trend", 0.0)
    momentum_feature = candidate.feature_scores.get("momentum", 0.0)
    b.trend_momentum_score = max(0.0, min(20.0, (trend_feature + momentum_feature) / 2))

    # Fake Breakout Risk (contradiction penalty): reuses Confidence
    # Engine's own contradictions list - a real signal, not fabricated.
    b.contradiction_penalty = min(20.0, len(candidate.contradictions) * 10.0)

    return b


# ---------------------------------------------------------------------
# Stage 5: SL / TP / RR / Leverage
# ---------------------------------------------------------------------

@dataclass
class TradePlan:
    entry_zone: tuple[float, float]
    reference_entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    rr: dict[str, float]
    risk_label: str
    suggested_leverage: float
    valid: bool
    rejection_reason: str | None = None


def build_trade_plan(tech: TechnicalContext, candles: list[Candle], market_regime: str = "unknown") -> TradePlan:
    entry_low = tech.recent_high  # breakout level itself
    entry_high = tech.current_price
    reference_entry = (entry_low + entry_high) / 2 if entry_low else tech.current_price

    # Stop Loss: below both the recent swing low AND a small ATR
    # buffer under the breakout level, whichever is lower (more
    # conservative) - a real technical placement (item #6), not an
    # arbitrary percentage.
    atr_multiplier = REGIME_ATR_MULTIPLIERS.get(market_regime, 2.0)
    atr_buffer = (tech.atr or 0) * atr_multiplier
    sl_candidates = [v for v in [tech.swing_low, (tech.recent_high or 0) - atr_buffer] if v and v > 0]
    valid_sls = [s for s in sl_candidates if (reference_entry - s) / reference_entry * 100 <= MAX_STOP_DISTANCE_PCT]
    stop_loss = min(valid_sls) if valid_sls else (max(sl_candidates) if sl_candidates else reference_entry * 0.95)

    stop_distance_pct = (reference_entry - stop_loss) / reference_entry * 100
    if stop_distance_pct <= 0 or stop_distance_pct > MAX_STOP_DISTANCE_PCT:
        return TradePlan((0, 0), 0, 0, 0, 0, 0, {}, "N/A", 0, valid=False,
                          rejection_reason=f"Stop mesafesi gecersiz veya asiri genis (%{stop_distance_pct:.1f})")

    # TP1/TP2/TP3: recent swing highs beyond the breakout level as real
    # resistance references (item #7), supplemented with ATR multiples
    # when the candle window doesn't offer enough distinct levels -
    # honestly a simple approach, not full market-structure detection.
    risk = reference_entry - stop_loss
    highs_beyond = sorted({c.high for c in candles if c.high > entry_high}, reverse=False)
    structural_tps = [h for h in highs_beyond if h > reference_entry][:3]

    atr = tech.atr or (risk * 0.6)
    fallback_tps = [reference_entry + risk * 1.8, reference_entry + risk * 3.0, reference_entry + risk * 4.5]

    tps = list(structural_tps) + fallback_tps
    tps = sorted(set(round(t, 10) for t in tps if t > reference_entry))
    tp1 = next((t for t in tps if t >= reference_entry + risk * 1.2), fallback_tps[0])
    tp2 = next((t for t in tps if t > tp1 and t >= reference_entry + risk * 2.5), fallback_tps[1])
    tp3 = next((t for t in tps if t > tp2), fallback_tps[2])

    rr_tp1 = (tp1 - reference_entry) / risk
    rr_tp2 = (tp2 - reference_entry) / risk
    rr_tp3 = (tp3 - reference_entry) / risk

    if rr_tp1 < MIN_RR_TP1:
        return TradePlan((0, 0), 0, 0, 0, 0, 0, {}, "N/A", 0, valid=False,
                          rejection_reason=f"Risk/Getiri yetersiz (TP1 = 1:{rr_tp1:.1f})")

    risk_label = "DÜŞÜK" if stop_distance_pct <= 3 else "ORTA" if stop_distance_pct <= 5.5 else "YÜKSEK"

    # Leverage: conservative, risk-based sizing - NOT boosted just
    # because confidence is high (explicit instruction #9).
    leverage = min(MAX_SUGGESTED_LEVERAGE, ASSUMED_RISK_PER_TRADE_PCT / stop_distance_pct * 10)
    leverage = max(1.0, round(leverage, 1))

    return TradePlan(
        entry_zone=(round(entry_low, 10), round(entry_high, 10)) if entry_low else (reference_entry, reference_entry),
        reference_entry=round(reference_entry, 10),
        stop_loss=round(stop_loss, 10),
        tp1=round(tp1, 10),
        tp2=round(tp2, 10),
        tp3=round(tp3, 10),
        rr={"tp1": round(rr_tp1, 2), "tp2": round(rr_tp2, 2), "tp3": round(rr_tp3, 2)},
        risk_label=risk_label,
        suggested_leverage=leverage,
        valid=True,
    )


# ---------------------------------------------------------------------
# Stage 6: Best-candidate selection
# ---------------------------------------------------------------------

@dataclass
class CandidateEvaluation:
    """Everything Entry Engine worked out for ONE Top15 candidate in a
    single evaluation pass - the decision AND the inputs behind it.
    Produced once by evaluate_top15() and reused for the selection, the
    --explain output and the entry_candidates log, so what gets logged
    is exactly what the decision was made from (never a second,
    possibly-different recomputation).
    """
    candidate: Top15Candidate
    tech: TechnicalContext | None
    structural: StructuralHealthResult
    htf: HTFAlignmentResult
    breakdown: EntryQualityBreakdown
    plan: TradePlan | None
    live_price: float
    selected: bool = False


async def evaluate_top15(candidates: list[Top15Candidate]) -> tuple[EntrySignal | None, list[CandidateEvaluation]]:
    """Fetches each timeframe exactly ONCE for the Top15 symbols
    (previously run() and select_best_entry() each fetched everything,
    doubling API calls and allowing the --explain table and the actual
    decision to be computed from two slightly different snapshots),
    scores every candidate, and picks the single best eligible LONG.

    Returns (signal_or_None, evaluations_for_all_candidates). Selection
    logic is unchanged: highest Entry Quality among candidates that
    clear ENTRY_QUALITY_THRESHOLD, have a confirmed breakout, and
    produce a valid trade plan.
    """
    if not candidates:
        return None, []

    symbols = [c.symbol for c in candidates]
    candles_by_symbol = await fetch_top15_candles(symbols)
    # Structural Health (365D ATH distance) - same fetch path, same
    # look-ahead protection, just a different timeframe/window. Only
    # 15 extra fetches (Top15 symbols only), never a fresh 530-symbol
    # scan, per the approved specification.
    daily_candles_by_symbol = await fetch_top15_candles(symbols, timeframe="1d", limit=STRUCTURAL_HEALTH_MIN_DAYS)
    # HTF Alignment V2 (4H context component) - same fetch path, same
    # look-ahead protection. 15 more Top15-only fetches.
    candles_4h_by_symbol = await fetch_top15_candles(symbols, timeframe="4h", limit=60)

    evaluations: list[CandidateEvaluation] = []
    for c in candidates:
        candles = candles_by_symbol.get(c.symbol, [])
        tech = analyze_technicals(candles)
        current_price = tech.current_price if tech is not None else (c.price_at_scan or 0.0)
        structural = analyze_structural_health(daily_candles_by_symbol.get(c.symbol, []), current_price)
        htf = analyze_htf_alignment(candles, candles_4h_by_symbol.get(c.symbol, []), current_price)
        breakdown = score_entry_quality(c, tech, structural, htf)
        plan = None
        if tech is not None and breakdown.total >= ENTRY_QUALITY_THRESHOLD and tech.breakout_confirmed:
            plan = build_trade_plan(tech, candles, market_regime=c.market_regime or "unknown")
            if not plan.valid:
                plan = None
        evaluations.append(CandidateEvaluation(
            candidate=c, tech=tech, structural=structural, htf=htf,
            breakdown=breakdown, plan=plan, live_price=current_price,
        ))

    eligible = [e for e in evaluations if e.plan is not None]
    if not eligible:
        return None, evaluations

    # --- NEWS & SENTIMENT FILTER KALDIRILDI (Kullanici istegi) ---

    if not eligible:
        return None, evaluations

    # Best candidate: highest Entry Quality wins (item #4's own worked
    # example - "Coin B (94) wins over A (82) and C (77)"). max() keeps
    # the first of equal totals, same as the previous stable sort.
    best = max(eligible, key=lambda e: e.breakdown.total)
    best.selected = True
    c, bd, tech, plan = best.candidate, best.breakdown, best.tech, best.plan

    signal = EntrySignal(
        symbol=c.symbol,
        scan_id=c.scan_id,
        direction="LONG",
        qde_score=c.qde_score,
        confidence=c.confidence,
        entry_quality=bd.total,
        entry_zone=plan.entry_zone,
        reference_entry=plan.reference_entry,
        stop_loss=plan.stop_loss,
        tp1=plan.tp1,
        tp2=plan.tp2,
        tp3=plan.tp3,
        risk_label=plan.risk_label,
        rr=plan.rr,
        suggested_leverage=plan.suggested_leverage,
        breakout_confirmed=tech.breakout_confirmed,
        retest_confirmed=tech.retest_confirmed,
        volume_confirmed=c.feature_scores.get("volume", 0.0) > 0,
        higher_tf_aligned=bd.higher_tf_score >= bd.htf_points_possible * 0.6
        if bd.htf_points_possible > 0 else False,
        breakdown=bd,
    )
    return signal, evaluations


async def select_best_entry(candidates: list[Top15Candidate]) -> EntrySignal | None:
    """Backward-compatible wrapper: returns only the selected signal."""
    signal, _ = await evaluate_top15(candidates)
    return signal


# ---------------------------------------------------------------------
# Stage 7: Turkish output formatting
# ---------------------------------------------------------------------

def _fmt_price(v: float) -> str:
    return f"{v:.10g}"


def format_signal(signal: EntrySignal) -> str:
    lines = [
        "=" * 40,
        "          QDE LONG SİNYALİ".center(40) if False else "          QDE LONG SİNYALİ",
        "=" * 40,
        "",
        f"Coin             : {signal.symbol}",
        f"Yön              : {signal.direction}",
        "",
        f"QDE Skoru        : {signal.qde_score:.0f}/100",
        f"Güven            : %{signal.confidence:.0f}",
        f"Giriş Kalitesi   : {signal.entry_quality:.0f}/100",
        "",
        f"Giriş Bölgesi    : {_fmt_price(signal.entry_zone[0])} - {_fmt_price(signal.entry_zone[1])}",
        f"Referans Giriş   : {_fmt_price(signal.reference_entry)}",
        "",
        f"STOP LOSS        : {_fmt_price(signal.stop_loss)}",
        "",
        f"TP1              : {_fmt_price(signal.tp1)}",
        f"TP2              : {_fmt_price(signal.tp2)}",
        f"TP3              : {_fmt_price(signal.tp3)}",
        "",
        f"Risk             : {signal.risk_label}",
        "",
        "Risk/Getiri:",
        f"TP1 : 1:{signal.rr['tp1']:.1f}",
        f"TP2 : 1:{signal.rr['tp2']:.1f}",
        f"TP3 : 1:{signal.rr['tp3']:.1f}",
        "",
        f"Önerilen Kaldıraç : {signal.suggested_leverage:.0f}x",
        "",
        f"Kırılım          : {'ONAYLI' if signal.breakout_confirmed else 'YOK'}",
        f"Retest           : {'ONAYLI' if signal.retest_confirmed else 'YOK'}",
        f"Hacim            : {'ONAYLI' if signal.volume_confirmed else 'ZAYIF'}",
        "",
    ]
    lines.extend(_format_htf_block(signal.breakdown))
    lines.extend(_format_structural_health_block(signal.breakdown))
    lines.extend([
        f"Sinyal Durumu    : {signal.status}",
        "",
        "=" * 40,
    ])
    return "\n".join(lines)


_STRUCTURAL_STATUS_LABELS = {
    "SAGLIKLI": "🟢 SAĞLIKLI",
    "DIKKAT": "⚠️ DİKKAT",
    "YUKSEK_RISK": "🔴 YÜKSEK RİSK",
    "COK_YUKSEK_RISK": "🔴 ÇOK YÜKSEK RİSK",
    "VERI_YETERSIZ": "VERİ YETERSİZ",
}


def _format_htf_block(breakdown: EntryQualityBreakdown) -> list[str]:
    """Shared by format_signal and format_explain - matches the user's
    own approved example layout exactly:
        1H Trend:       BULLISH
        1H Structure:   NEUTRAL
        4H Context:     INSUFFICIENT_DATA
        HTF Score:      5/7
        HTF Status:     PARTIAL
    """
    return [
        f"1H Trend         : {breakdown.htf_trend_1h_label}",
        f"1H Yapı          : {breakdown.htf_structure_1h_label}",
        f"4H Bağlam        : {breakdown.htf_context_4h_label}",
        f"HTF Puanı        : {breakdown.higher_tf_score:.1f}/{breakdown.htf_points_possible:.1f}",
        f"HTF Durumu       : {breakdown.htf_status}",
        "",
    ]


def _format_structural_health_block(breakdown: EntryQualityBreakdown) -> list[str]:
    """Shared by format_signal and format_explain - a single source of
    truth for how Structural Health is rendered, per the approved
    specification's exact example layout.
    """
    label = _STRUCTURAL_STATUS_LABELS.get(breakdown.structural_status, breakdown.structural_status)
    lines = [f"Yapısal Sağlık   : {label}"]
    if breakdown.structural_status != "VERI_YETERSIZ":
        lines.append(f"365G ATH         : {_fmt_price(breakdown.structural_ath)}")
        lines.append(f"365G ATH Uzaklığı: {breakdown.structural_ath_distance_pct:+.1f}%")
        if breakdown.structural_penalty > 0:
            lines.append(f"Ceza             : -{breakdown.structural_penalty:.1f}")
    lines.append("")
    return lines


def format_no_signal() -> str:
    return "\n".join([
        "=" * 40,
        "           QDE SİNYAL SONUCU",
        "=" * 40,
        "",
        "Top15 içerisinde şu anda",
        "giriş kriterlerini karşılayan",
        "uygun bir LONG işlemi bulunamadı.",
        "",
        "İŞLEM YOK",
        "",
        "=" * 40,
    ])


def format_explain(candidates_scored: list[tuple[str, EntryQualityBreakdown]]) -> str:
    lines = ["", "-- Giris Kalitesi Detayi (tum Top15) --"]
    for symbol, breakdown in candidates_scored:
        d = breakdown.as_dict()
        structural_note = (
            f" yapisal={d['structural_status']}({d['structural_ath_distance_pct']:+.1f}%, ceza={d['structural_penalty']:.1f})"
            if d["structural_status"] != "VERI_YETERSIZ"
            else " yapisal=VERI_YETERSIZ"
        )
        htf_note = f" htf={d['htf_status']}({d['htf_points']}: 1h={d['htf_trend_1h']}/{d['htf_structure_1h']} 4h={d['htf_context_4h']})"
        lines.append(
            f"{symbol:20s} toplam={d['total']:5.1f}{structural_note}  "
            f"kirilim={d['breakout']:4.1f} retest={d['retest']:4.1f} "
            f"uzama_cezasi={d['extension_penalty']:5.1f} hacim={d['volume']:4.1f} "
            f"trend_mom={d['trend_momentum']:4.1f} ust_tf={d['higher_timeframe']:4.1f} "
            f"celiski_cezasi={d['contradiction_penalty']:5.1f}{htf_note}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Stage 8: Persistence
# ---------------------------------------------------------------------

def record_signal(signal: EntrySignal) -> None:
    conn = _connect_signal_db()
    try:
        conn.execute(
            """
            INSERT INTO entry_signals (
                scan_id, timestamp, symbol, direction, qde_score, confidence,
                entry_quality, entry_zone_low, entry_zone_high, reference_entry,
                stop_loss, tp1, tp2, tp3, risk_label, rr_tp1, rr_tp2, rr_tp3,
                suggested_leverage, breakout_confirmed, retest_confirmed,
                volume_confirmed, higher_tf_aligned, signal_status, breakdown_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                signal.scan_id,
                datetime.now(timezone.utc).isoformat(),
                signal.symbol,
                signal.direction,
                signal.qde_score,
                signal.confidence,
                signal.entry_quality,
                signal.entry_zone[0],
                signal.entry_zone[1],
                signal.reference_entry,
                signal.stop_loss,
                signal.tp1,
                signal.tp2,
                signal.tp3,
                signal.risk_label,
                signal.rr["tp1"],
                signal.rr["tp2"],
                signal.rr["tp3"],
                signal.suggested_leverage,
                int(signal.breakout_confirmed),
                int(signal.retest_confirmed),
                int(signal.volume_confirmed),
                int(signal.higher_tf_aligned),
                signal.status,
                json.dumps(signal.breakdown.as_dict()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_no_signal(scan_id: str) -> None:
    conn = _connect_signal_db()
    try:
        conn.execute(
            """INSERT INTO entry_signals (scan_id, timestamp, symbol, direction, signal_status)
               VALUES (?, ?, 'NONE', 'NONE', 'ISLEM_YOK')""",
            (scan_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


_CANDIDATE_INSERT_SQL = """
INSERT INTO entry_candidates (
    run_timestamp, scan_id, setup_id, symbol, rank, qde_score, qde_confidence,
    price_at_scan, live_price, selected, entry_quality,
    breakout_confirmed, retest_confirmed, breakout_level, atr, extension_atr,
    breakout_candles_ago, swing_low, candles_1h,
    structural_status, structural_ath, structural_ath_distance_pct, structural_penalty,
    htf_status, htf_trend_1h_score, htf_structure_1h_score, htf_context_4h_score,
    htf_points_earned, htf_points_possible, breakdown_json
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def record_candidates(run_timestamp: str, evaluations: list[CandidateEvaluation]) -> int:
    """Persists the full evaluation of EVERY Top15 candidate (not just
    the selected one) so Entry Quality components can later be joined,
    via setup_id, to outcome_engine.py's fixed-horizon outcomes - the
    only honest way to tell whether the 75 threshold, the extension
    bands or the retest weight are actually well calibrated.

    Logging only: nothing here is read back by scoring. Technical
    columns are NULL (not 0) when 1h data was unavailable for a
    candidate - "not computed" must stay distinguishable from "zero".
    Returns the number of rows written.
    """
    if not evaluations:
        return 0
    rows = []
    for e in evaluations:
        c, t, st, h, b = e.candidate, e.tech, e.structural, e.htf, e.breakdown
        rows.append((
            run_timestamp, c.scan_id, c.setup_id, c.symbol, c.rank, c.qde_score, c.confidence,
            c.price_at_scan, e.live_price, int(e.selected), b.total,
            None if t is None else int(t.breakout_confirmed),
            None if t is None else int(t.retest_confirmed),
            None if t is None else t.recent_high,
            None if t is None else t.atr,
            None if t is None else t.extension_atr_multiples,
            None if t is None else t.breakout_candles_ago,
            None if t is None else t.swing_low,
            None if t is None else t.candles_available,
            st.status, st.ath, st.ath_distance_pct, st.penalty,
            h.status, h.trend_1h_score, h.structure_1h_score, h.context_4h_score,
            h.points_earned, h.points_possible, json.dumps(b.as_dict()),
        ))
    conn = _connect_signal_db()
    try:
        conn.executemany(_CANDIDATE_INSERT_SQL, rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

async def run(explain: bool = False) -> str:
    candidates = fetch_latest_top15()
    if not candidates:
        return "Henuz bir QDE taramasi/Top15 bulunamadi. Once 'python main.py' calistirin."

    signal, evaluations = await evaluate_top15(candidates)

    # Log every candidate's full evaluation. A logging failure must never
    # block the signal output - but it must also never be silent (a
    # swallowed write error is exactly how data quietly goes missing),
    # so it is surfaced as a visible one-line warning instead.
    log_warning = ""
    try:
        record_candidates(datetime.now(timezone.utc).isoformat(), evaluations)
    except Exception as exc:  # noqa: BLE001
        log_warning = f"UYARI: aday kaydi yazilamadi ({type(exc).__name__}: {exc})\n"

    output = log_warning
    if explain:
        output += format_explain([(e.candidate.symbol, e.breakdown) for e in evaluations]) + "\n"

    if signal is None:
        record_no_signal(candidates[0].scan_id)
        output += format_no_signal()
    else:
        record_signal(signal)
        output += format_signal(signal)

    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="QDE Entry Engine")
    parser.add_argument("--explain", action="store_true", help="Tum Top15 icin Giris Kalitesi detayini da goster")
    args = parser.parse_args()
    print(asyncio.run(run(explain=args.explain)))


if __name__ == "__main__":
    main()
