"""
QDE V1.1 - tie_break_engine.py

QDE Tie-Break Ranking Engine (standalone plugin module).

PURPOSE
    Many symbols land on the exact same raw_score/confidence (e.g. four
    coins all at Score=100, Confidence=100). The Scoring/Confidence
    Engines were never designed to differentiate within that tie - this
    module adds a second-stage, additive-only re-ranking layer that
    breaks ties using finer-grained signal already computed by the
    indicators, without ever changing Setup.raw_score or
    Result.confidence.

PIPELINE POSITION (unchanged from the design doc)
    Hard Filter -> Scoring Engine -> Confidence Engine -> Tie Break Engine -> Final Ranking
    This module only runs AFTER confidence is known, and only reorders
    symbols that are already tied at or above the configured
    tie_score_threshold. It never runs for symbols below the threshold.

WEIGHTS ARE CONFIG-DRIVEN, NOT HARDCODED
    Every category weight (trend/momentum/volume/breakout/risk) and the
    tie_score_threshold live in config/tie_break_weights.json - never
    as a magic number in this file. tie_break_optimizer.py (a separate,
    standalone module) can grid-search this file's values against real
    historical outcomes and rewrite it, the same way learning_engine.py
    already does for config/weights.json - see that module for the
    optimization side of this design.

HONESTY POLICY (same standard as audit_engine.py's Missing Data Report)
    The request lists several criteria this codebase cannot genuinely
    compute today: MACD histogram, ATR, true breakout/retest detection,
    fake-breakout probability, and order-book depth (QDE only ever
    fetches OHLCV/ticker/funding/OI - never a full order book). Rather
    than fabricate these, each sub-score below states exactly what real
    data it uses and, where a requested criterion isn't available,
    what honest proxy (if any) stands in for it. See each _score_*
    function's docstring.

WHAT THIS MODULE DOES NOT DO
    - Does not change raw_score, confidence, risk_grade, or uniqueness.
    - Does not run a new scan or call the exchange.
    - Does not claim to be backtested. True fixed-horizon forward
      validation (Document 12 SS7: Sharpe/Profit Factor/Win Rate) needs
      infrastructure already flagged as missing in audit_engine.py's
      Missing Data Report - it does not exist yet. What this module
      DOES do is record every tie-break decision (tie_break_history.db)
      so tie_break_optimizer.py can validate/tune weights against real
      subsequent price action once enough history accumulates.

Dependency level: 1 (depends on models.market_data, models.feature -
read-only, never imports engines/scoring.py or engines/confidence.py,
and neither of those modules imports this one).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models.feature import Feature
from models.market_data import MarketData

_ROOT = Path(__file__).resolve().parent
_WEIGHTS_CONFIG_PATH = _ROOT / "config" / "tie_break_weights.json"
_WEIGHTS_BACKUP_PATH = _ROOT / "config" / "tie_break_weights_backup.json"
_HISTORY_DB_PATH = _ROOT / "tie_break_history.db"

# Safety-net defaults, used ONLY if config/tie_break_weights.json is
# missing or unreadable - never the primary source of truth. Mirrors
# the values the design doc itself proposed as a starting point.
_FALLBACK_WEIGHTS = {
    "trend": 0.30,
    "momentum": 0.25,
    "volume": 0.20,
    "breakout": 0.15,
    "risk": 0.10,
}
_FALLBACK_THRESHOLD = 95.0

_PUMP_24H_THRESHOLD = 20.0  # Document 12's own example: ">20% pump in 24h"
_WICK_RATIO_DANGER = 2.5  # wick length vs body length, above which a candle looks "extended"

_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS tie_break_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    scan_timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    tie_group_key TEXT NOT NULL,
    price_at_scan REAL,
    raw_trend REAL,
    raw_momentum REAL,
    raw_volume REAL,
    raw_breakout REAL,
    raw_risk REAL,
    weighted_total REAL,
    weights_used_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tbh_scan ON tie_break_history (scan_id);
CREATE INDEX IF NOT EXISTS idx_tbh_group ON tie_break_history (tie_group_key);
CREATE INDEX IF NOT EXISTS idx_tbh_symbol ON tie_break_history (symbol);
"""


def load_weights() -> dict[str, float]:
    """Reads config/tie_break_weights.json. Falls back to documented
    defaults (never silently different behavior - a warning-worthy
    event, but not fatal) only if the file is missing/corrupt.
    """
    try:
        data = json.loads(_WEIGHTS_CONFIG_PATH.read_text())
        return {k: float(data[k]) for k in _FALLBACK_WEIGHTS}
    except (FileNotFoundError, KeyError, json.JSONDecodeError, ValueError):
        return dict(_FALLBACK_WEIGHTS)


def load_threshold() -> float:
    try:
        data = json.loads(_WEIGHTS_CONFIG_PATH.read_text())
        return float(data.get("tie_score_threshold", _FALLBACK_THRESHOLD))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return _FALLBACK_THRESHOLD


TIE_SCORE_THRESHOLD = load_threshold()


@dataclass
class TieBreakInputs:
    """Lightweight, per-symbol snapshot captured while MarketData is
    still in scope (inside main.QDEPipeline._process_one), so the full
    MarketData/candle history doesn't need to be held in memory until
    ranking runs. Cheap to keep for all ~500 scanned symbols.
    """

    symbol: str
    trend_bullish_alignment: bool | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    rsi: float | None = None
    volume_ratio: float | None = None
    volatility_percentile: float | None = None
    pct_change_24h: float | None = None
    avg_wick_ratio: float | None = None
    quote_volume: float | None = None
    spread: float | None = None
    price_at_scan: float | None = None


@dataclass
class TieBreakResult:
    symbol: str
    trend_score: float
    momentum_score: float
    volume_score: float
    breakout_score: float
    risk_penalty: float
    total: float
    breakdown: dict[str, float] = field(default_factory=dict)
    # Raw (0-10 scale, unweighted) category scores - the inputs to the
    # weighted total above. tie_break_optimizer.py grid-searches
    # candidate weight vectors purely by recombining these stored raw
    # values against historical outcomes, without needing to reload
    # MarketData or recompute indicators for every candidate.
    raw_trend: float = 0.0
    raw_momentum: float = 0.0
    raw_volume: float = 0.0
    raw_breakout: float = 0.0
    raw_risk: float = 0.0

    def explain(self) -> str:
        lines = [
            f"Trend ............ {self.trend_score:+.1f}",
            f"Momentum ......... {self.momentum_score:+.1f}",
            f"Volume ........... {self.volume_score:+.1f}",
            f"Breakout ......... {self.breakout_score:+.1f}",
            f"Risk ............. {-self.risk_penalty:+.1f}",
            f"Toplam ........... {self.total:+.1f}",
        ]
        return "\n".join(lines)


def build_inputs(symbol: str, market_data: MarketData, features: list[Feature]) -> TieBreakInputs:
    """Extracts everything the Tie-Break Engine needs from data that
    already exists at the point _process_one computes it - no new
    exchange calls, no new indicators.
    """
    by_name = {f.name: f for f in features}
    inputs = TieBreakInputs(symbol=symbol)

    trend = by_name.get("trend")
    if trend:
        inputs.trend_bullish_alignment = trend.metadata.get("bullish_alignment")
        inputs.ema20 = trend.metadata.get("ema20")
        inputs.ema50 = trend.metadata.get("ema50")
        inputs.ema200 = trend.metadata.get("ema200")

    momentum = by_name.get("momentum")
    if momentum:
        inputs.rsi = momentum.metadata.get("rsi")

    volume = by_name.get("volume")
    if volume:
        inputs.volume_ratio = volume.metadata.get("ratio")

    volatility = by_name.get("volatility")
    if volatility:
        inputs.volatility_percentile = volatility.metadata.get("percentile")

    inputs.pct_change_24h = market_data.ticker.get("percentage")
    inputs.quote_volume = market_data.quote_volume()
    inputs.spread = market_data.spread()
    inputs.avg_wick_ratio = _compute_avg_wick_ratio(market_data)
    inputs.price_at_scan = market_data.ticker.get("last") or market_data.mark_price.get("mark_price")

    return inputs


def _compute_avg_wick_ratio(market_data: MarketData, lookback: int = 5) -> float | None:
    """Average (upper_wick + lower_wick) / body across the most recent
    `lookback` 15m candles - a real, computable proxy for "extended /
    exhausted" candles (Document 12 SS7's "çok uzun fitiller"), using
    only OHLCV data QDE already has. Not a substitute for true
    candlestick pattern recognition (not implemented - see
    audit_engine.py's Missing Data Report), just a cheap geometric
    signal in the same spirit.
    """
    tf = market_data.get_timeframe("15m")
    if tf is None or len(tf.candles) < lookback:
        return None
    recent = tf.candles[-lookback:]
    ratios = []
    for c in recent:
        body = abs(c.close - c.open)
        if body == 0:
            continue
        upper_wick = c.high - max(c.open, c.close)
        lower_wick = min(c.open, c.close) - c.low
        ratios.append((upper_wick + lower_wick) / body)
    if not ratios:
        return None
    return sum(ratios) / len(ratios)


class TieBreakEngine:
    """Computes an additive, explainable tie-break score for a symbol.
    Never touches Setup/Result - callers attach the output separately
    and use it only as a secondary sort key among already-tied
    symbols (see apply_tie_break below).

    Weights default to config/tie_break_weights.json (load_weights()),
    never a hardcoded dict - tie_break_optimizer.py rewrites that file;
    this class simply reads whatever is currently there. Pass an
    explicit `weights` dict only for testing/what-if analysis.
    """

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = weights if weights is not None else load_weights()

    def calculate(self, inputs: TieBreakInputs) -> TieBreakResult:
        raw_trend = self._score_trend(inputs)
        raw_momentum = self._score_momentum(inputs)
        raw_volume = self._score_volume(inputs)
        raw_breakout = self._score_breakout(inputs)
        raw_risk = self._score_risk(inputs)

        trend = raw_trend * self.weights["trend"]
        momentum = raw_momentum * self.weights["momentum"]
        volume = raw_volume * self.weights["volume"]
        breakout = raw_breakout * self.weights["breakout"]
        risk = raw_risk * self.weights["risk"]

        total = trend + momentum + volume + breakout - risk

        return TieBreakResult(
            symbol=inputs.symbol,
            trend_score=trend,
            momentum_score=momentum,
            volume_score=volume,
            breakout_score=breakout,
            risk_penalty=risk,
            total=round(total, 2),
            breakdown={
                "trend": round(trend, 2),
                "momentum": round(momentum, 2),
                "volume": round(volume, 2),
                "breakout": round(breakout, 2),
                "risk": round(-risk, 2),
            },
            raw_trend=raw_trend,
            raw_momentum=raw_momentum,
            raw_volume=raw_volume,
            raw_breakout=raw_breakout,
            raw_risk=raw_risk,
        )

    @staticmethod
    def _score_trend(i: TieBreakInputs) -> float:
        """Real data: EMA20/50/200 alignment + relative spread between
        them as a continuous "how strong/fresh is the alignment" proxy
        (wider EMA spread with correct ordering = more established
        trend continuity - Document 12 SS1's "trend devamlılığı").
        No true multi-candle trend-stability window is computed (would
        need to re-run EMA alignment across a rolling history, not
        just the latest point) - flagged as a real gap, not faked.
        """
        if i.ema20 is None or i.ema50 is None or i.ema200 is None:
            return 0.0
        if not i.trend_bullish_alignment:
            return 0.0
        spread_pct = ((i.ema20 - i.ema200) / i.ema200 * 100) if i.ema200 else 0.0
        return max(0.0, min(10.0, spread_pct))

    @staticmethod
    def _score_momentum(i: TieBreakInputs) -> float:
        """Real data: RSI distance from the ideal 45-65 zone
        (indicators/momentum.py's own healthy-range definition, reused
        here for consistency). MACD histogram / momentum acceleration
        are NOT computed anywhere in this codebase (see Missing Data
        Report) - this is a single-point RSI read, not a slope.
        """
        if i.rsi is None:
            return 0.0
        if 45 <= i.rsi <= 65:
            return 10.0 - abs(i.rsi - 55) * 0.3
        return max(0.0, 5.0 - abs(i.rsi - 55) * 0.2)

    @staticmethod
    def _score_volume(i: TieBreakInputs) -> float:
        """Real data: the Volume indicator's own ratio (current candle
        vs recent average). Per Document 12 SS3's explicit preference
        for sustained participation over a single-candle spike, this
        rewards a moderate, sustained ratio (1.3-2.5x) more than an
        extreme one-candle spike (>4x, which more likely reflects a
        single large order than genuine sustained interest).
        """
        if i.volume_ratio is None:
            return 0.0
        if 1.3 <= i.volume_ratio <= 2.5:
            return 10.0
        if i.volume_ratio > 2.5:
            return max(3.0, 10.0 - (i.volume_ratio - 2.5) * 1.5)
        return max(0.0, (i.volume_ratio - 1.0) * 10.0)

    @staticmethod
    def _score_breakout(i: TieBreakInputs) -> float:
        """APPROXIMATION, explicitly not the real thing: true breakout
        quality (Document 12 SS4 - candles since breakout, retest,
        resistance validation, fake-breakout probability) needs support/
        resistance level tracking this codebase does not have. As a
        structurally-related, honestly-labeled proxy, this rewards low
        volatility-percentile (i.e. the symbol was recently compressed
        - Document 03's own "Volatility Compression" concept) combined
        with volume expansion, which is the same "coiled spring
        releasing" pattern a genuine fresh breakout would show. It is
        not a validated breakout indicator.
        """
        if i.volatility_percentile is None or i.volume_ratio is None:
            return 0.0
        was_compressed = max(0.0, (50.0 - i.volatility_percentile) / 50.0)  # 0..1
        expanding_now = max(0.0, min(1.0, (i.volume_ratio - 1.0) / 2.0))  # 0..1
        return 10.0 * was_compressed * expanding_now

    @staticmethod
    def _score_risk(i: TieBreakInputs) -> float:
        """Real data: 24h % change (already-pumped penalty, exactly
        Document 12 SS7's own example threshold), candle wick geometry
        (extended-wick penalty), and spread (liquidity-risk penalty
        using data already fetched - order book depth itself is NOT
        available, see Missing Data Report). Returns a POSITIVE
        magnitude - the caller subtracts it, per Document 12 SS "ceza
        sistemi ödül sisteminden güçlü olmamalı": capped at 10 so one
        risk flag can't override the other four categories combined.
        """
        penalty = 0.0
        if i.pct_change_24h is not None and abs(i.pct_change_24h) >= _PUMP_24H_THRESHOLD:
            penalty += min(5.0, (abs(i.pct_change_24h) - _PUMP_24H_THRESHOLD) * 0.2 + 3.0)
        if i.avg_wick_ratio is not None and i.avg_wick_ratio >= _WICK_RATIO_DANGER:
            penalty += min(3.0, (i.avg_wick_ratio - _WICK_RATIO_DANGER) * 0.5 + 1.5)
        if i.spread is not None and i.spread > 0.003:
            penalty += min(2.0, i.spread * 300)
        return min(10.0, penalty)


def apply_tie_break(
    ranked_symbols: list[str],
    raw_scores: dict[str, float],
    tie_keys: dict[str, Any],
    inputs_by_symbol: dict[str, TieBreakInputs],
    engine: TieBreakEngine | None = None,
    threshold: float = TIE_SCORE_THRESHOLD,
) -> tuple[list[str], dict[str, TieBreakResult]]:
    """Re-orders `ranked_symbols` (already sorted by the existing
    confidence -> score -> uniqueness -> ... rule) so that symbols
    sharing an identical tie_key (typically (confidence, raw_score))
    with raw_score at or above `threshold` are additionally sorted by
    their tie-break total, highest first. Everything else is left in
    its original relative order untouched.

    Returns the new symbol order plus a dict of every computed
    TieBreakResult (for explainability/display) - callers never need
    to mutate Setup or Result to use this.
    """
    engine = engine or TieBreakEngine()
    tie_break_results: dict[str, TieBreakResult] = {}

    output: list[str] = []
    i = 0
    n = len(ranked_symbols)
    while i < n:
        symbol = ranked_symbols[i]
        raw_score = raw_scores.get(symbol)
        if raw_score is None or raw_score < threshold:
            output.append(symbol)
            i += 1
            continue

        j = i
        group = []
        key = tie_keys.get(symbol)
        while j < n and tie_keys.get(ranked_symbols[j]) == key:
            group.append(ranked_symbols[j])
            j += 1

        if len(group) > 1:
            for sym in group:
                inputs = inputs_by_symbol.get(sym)
                tie_break_results[sym] = (
                    engine.calculate(inputs) if inputs is not None else TieBreakResult(sym, 0, 0, 0, 0, 0, 0.0)
                )
            group.sort(key=lambda s: tie_break_results[s].total, reverse=True)

        output.extend(group)
        i = j

    return output, tie_break_results


# ---------------------------------------------------------------------
# History recording (feeds tie_break_optimizer.py)
# ---------------------------------------------------------------------

def _connect_history_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_HISTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_HISTORY_SCHEMA)
    return conn


def record_tie_break_results(
    scan_id: str,
    scan_timestamp: str,
    tie_break_results: dict[str, TieBreakResult],
    inputs_by_symbol: dict[str, TieBreakInputs],
    tie_keys: dict[str, Any],
    weights_used: dict[str, float],
) -> None:
    """Persists every tie-break decision actually made in a scan (i.e.
    only symbols that were part of a real tie - most scans, this is a
    handful of rows, not hundreds). Records the RAW (unweighted)
    sub-scores plus the weights that were active at the time, so
    tie_break_optimizer.py can later ask "what would the ranking have
    been under different weights?" without needing to re-fetch market
    data or recompute indicators.

    Safe to call unconditionally - failures are the caller's
    responsibility to isolate (matches the pattern used by
    performance_tracker.record_top15 and audit_engine.capture_snapshot).
    """
    if not tie_break_results:
        return

    conn = _connect_history_db()
    try:
        rows = []
        for symbol, result in tie_break_results.items():
            inputs = inputs_by_symbol.get(symbol)
            tie_group_key = str(tie_keys.get(symbol, ""))
            rows.append(
                (
                    scan_id,
                    scan_timestamp,
                    symbol,
                    tie_group_key,
                    inputs.price_at_scan if inputs else None,
                    result.raw_trend,
                    result.raw_momentum,
                    result.raw_volume,
                    result.raw_breakout,
                    result.raw_risk,
                    result.total,
                    json.dumps(weights_used),
                )
            )
        conn.executemany(
            """
            INSERT INTO tie_break_history (
                scan_id, scan_timestamp, symbol, tie_group_key, price_at_scan,
                raw_trend, raw_momentum, raw_volume, raw_breakout, raw_risk,
                weighted_total, weights_used_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
