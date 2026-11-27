"""
QDE V1.1 - tie_break_optimizer.py

QDE Tie-Break Weight Optimizer (standalone plugin module).

WHAT THIS MODULE DOES
    Reads every tie-break decision tie_break_engine.py has ever
    recorded (tie_break_history.db - raw, unweighted category scores
    per symbol per tie group), fetches each symbol's current price to
    compute a realized return, then grid-searches candidate weight
    vectors for config/tie_break_weights.json to find the combination
    that most often ranks symbols the way their actual subsequent
    performance did.

WHY GRID SEARCH, NOT BAYESIAN OPTIMIZATION (stated honestly up front)
    True Bayesian optimization (e.g. a Gaussian-process-based search)
    would need a new dependency (scikit-optimize or similar) not
    currently part of this project, and meaningfully more complexity
    for 5 parameters where a coarse grid is already exhaustive and
    fast (a few thousand candidate combinations, each just cheap
    arithmetic over already-stored raw scores - no re-fetching, no
    re-computation). Grid Search is implemented here, is real and
    tested, and is deterministic/explainable (Document 03 SS14's own
    Explainable AI Principle). Bayesian optimization is a documented,
    honest future upgrade (see BayesianOptimizer stub at the bottom of
    this file) - not claimed as already built.

WHAT "BACKTEST ENGINE" MEANS HERE (there isn't one - be precise)
    No dedicated Backtest Engine exists in this codebase (flagged
    already in audit_engine.py's Missing Data Report). This optimizer
    instead validates candidate weights against two real, already-
    existing data sources:
        1. tie_break_history.db (this module's own recording, written
           by tie_break_engine.record_tie_break_results every scan)
        2. Live current prices (fetched the same way
           performance_tracker.py already does, via ExchangeClient)
    This is a genuine, if simple, form of validation - not a
    fabricated "backtest passed" claim. See EVALUATION METHOD below.

EVALUATION METHOD: pairwise concordance
    For every historical tie group (symbols that were genuinely tied
    on confidence+score in the same scan), and for every pair of
    symbols (A, B) within that group where a realized return is now
    known for both: a candidate weight vector is "correct" for that
    pair if it ranks the symbol with the higher realized return above
    the other. The concordance rate (correct_pairs / total_pairs)
    across all historical tie groups is the score a weight vector is
    grid-searched to maximize. This is a standard, explainable ranking-
    quality metric (closely related to Kendall's tau / AUC), not a
    profit/loss backtest.

SAFETY GATES (mirrors learning_engine.py's established pattern)
    - MIN_PAIRS: a minimum number of scored pairs before any
      optimization is trusted (protects against small-sample noise -
      Document 12's own "Aşırı optimizasyon yapılmamalı").
    - MIN_IMPROVEMENT: the candidate weights must beat the current
      config's concordance by a real margin, not a coin-flip
      difference, before being written.
    - Every application is backed up first (config/tie_break_weights_backup.json)
      and logged (tie_break_optimization_log.json), mirroring
      learning_engine.py's audit trail.

Usage:
    python tie_break_optimizer.py --report      # grid search, print results, no write
    python tie_break_optimizer.py --apply       # grid search and write config if gates pass
    python tie_break_optimizer.py --rollback    # revert to the pre-optimization backup
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tie_break_engine as tbe

_ROOT = Path(__file__).resolve().parent
_LOG_PATH = _ROOT / "tie_break_optimization_log.json"

MIN_PAIRS = 30
MIN_IMPROVEMENT = 0.03  # candidate must beat current concordance by 3 percentage points
GRID_STEP = 0.10  # coarse grid: weights tested in 10% increments


@dataclass
class HistoryRow:
    scan_id: str
    symbol: str
    tie_group_key: str
    price_at_scan: float | None
    raw_trend: float
    raw_momentum: float
    raw_volume: float
    raw_breakout: float
    raw_risk: float
    current_price: float | None = None
    pct_change: float | None = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(tbe._HISTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(tbe._HISTORY_SCHEMA)
    return conn


def load_history() -> list[HistoryRow]:
    if not tbe._HISTORY_DB_PATH.exists():
        return []
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM tie_break_history").fetchall()
    finally:
        conn.close()
    return [
        HistoryRow(
            scan_id=r["scan_id"],
            symbol=r["symbol"],
            tie_group_key=f"{r['scan_id']}::{r['tie_group_key']}",  # scope groups to their own scan
            price_at_scan=r["price_at_scan"],
            raw_trend=r["raw_trend"] or 0.0,
            raw_momentum=r["raw_momentum"] or 0.0,
            raw_volume=r["raw_volume"] or 0.0,
            raw_breakout=r["raw_breakout"] or 0.0,
            raw_risk=r["raw_risk"] or 0.0,
        )
        for r in rows
    ]


async def _enrich_with_current_prices(rows: list[HistoryRow]) -> None:
    import performance_tracker as pt

    symbols = sorted(set(r.symbol for r in rows))
    if not symbols:
        return
    try:
        current_prices = await pt.fetch_current_prices(symbols)
    except Exception:
        current_prices = {s: None for s in symbols}
    for r in rows:
        r.current_price = current_prices.get(r.symbol)
        r.pct_change = pt.pct_change(r.price_at_scan, r.current_price)


def _build_pairs(rows: list[HistoryRow]) -> list[tuple[HistoryRow, HistoryRow]]:
    """Every within-tie-group pair where both symbols have a known
    realized return. Each pair is used to test whether a candidate
    weight vector ranks the actually-better performer higher.
    """
    by_group: dict[str, list[HistoryRow]] = {}
    for r in rows:
        if r.pct_change is None:
            continue
        by_group.setdefault(r.tie_group_key, []).append(r)

    pairs = []
    for group_rows in by_group.values():
        if len(group_rows) < 2:
            continue
        for a, b in itertools.combinations(group_rows, 2):
            if a.pct_change == b.pct_change:
                continue  # no ordering signal in a true tie of outcomes
            pairs.append((a, b))
    return pairs


def _weighted_total(row: HistoryRow, weights: dict[str, float]) -> float:
    return (
        row.raw_trend * weights["trend"]
        + row.raw_momentum * weights["momentum"]
        + row.raw_volume * weights["volume"]
        + row.raw_breakout * weights["breakout"]
        - row.raw_risk * weights["risk"]
    )


def concordance(pairs: list[tuple[HistoryRow, HistoryRow]], weights: dict[str, float]) -> float:
    """Fraction of pairs where the weight vector ranks the symbol with
    the higher realized return above the other. 0.5 = no better than
    random; 1.0 = perfect agreement with realized outcomes.
    """
    if not pairs:
        return 0.0
    correct = 0
    for a, b in pairs:
        better_by_outcome = a if a.pct_change > b.pct_change else b
        worse_by_outcome = b if better_by_outcome is a else a
        if _weighted_total(better_by_outcome, weights) > _weighted_total(worse_by_outcome, weights):
            correct += 1
    return correct / len(pairs)


def _generate_grid(step: float = GRID_STEP) -> list[dict[str, float]]:
    """Coarse grid over the 4 positive categories (constrained to sum
    to 1.0, matching the design doc's own percentage framing) crossed
    with a separate, smaller risk-weight grid (0.05-0.25, since risk
    is a penalty term, not part of the positive-category simplex).
    """
    steps = [round(x * step, 2) for x in range(1, int(1 / step))]  # e.g. 0.1..0.9
    candidates = []
    for t, m, v in itertools.product(steps, repeat=3):
        b = round(1.0 - t - m - v, 2)
        if 0.05 <= b <= 0.9:
            for risk in (0.05, 0.10, 0.15, 0.20, 0.25):
                candidates.append({"trend": t, "momentum": m, "volume": v, "breakout": b, "risk": risk})
    return candidates


def grid_search(pairs: list[tuple[HistoryRow, HistoryRow]]) -> tuple[dict[str, float], float, int]:
    """Returns (best_weights, best_concordance, candidates_tested)."""
    best_weights = tbe.load_weights()
    best_score = concordance(pairs, best_weights)
    tested = 0
    for candidate in _generate_grid():
        tested += 1
        score = concordance(pairs, candidate)
        if score > best_score:
            best_score = score
            best_weights = candidate
    return best_weights, best_score, tested


def _log_entry(entry: dict[str, Any]) -> None:
    log = []
    if _LOG_PATH.exists():
        try:
            log = json.loads(_LOG_PATH.read_text())
        except json.JSONDecodeError:
            log = []
    log.append(entry)
    _LOG_PATH.write_text(json.dumps(log, indent=2, ensure_ascii=False))


def apply_weights(new_weights: dict[str, float], current_concordance: float, new_concordance: float, pair_count: int) -> None:
    current = tbe.load_weights()
    tbe._WEIGHTS_BACKUP_PATH.write_text(json.dumps(current, indent=2))

    payload = dict(new_weights)
    payload["tie_score_threshold"] = tbe.load_threshold()
    tbe._WEIGHTS_CONFIG_PATH.write_text(json.dumps(payload, indent=2))

    _log_entry(
        {
            "tarih": datetime.now(timezone.utc).isoformat(),
            "action": "apply",
            "eski_agirliklar": current,
            "yeni_agirliklar": new_weights,
            "eski_concordance": round(current_concordance, 4),
            "yeni_concordance": round(new_concordance, 4),
            "kullanilan_cift_sayisi": pair_count,
        }
    )


def rollback() -> bool:
    if not tbe._WEIGHTS_BACKUP_PATH.exists():
        return False
    backup = json.loads(tbe._WEIGHTS_BACKUP_PATH.read_text())
    tbe._WEIGHTS_CONFIG_PATH.write_text(json.dumps(backup, indent=2))
    _log_entry({"tarih": datetime.now(timezone.utc).isoformat(), "action": "manual_rollback", "restored": backup})
    return True


async def run_optimization(apply: bool = False) -> str:
    rows = load_history()
    if not rows:
        return (
            "Henuz hic tie-break gecmisi yok (tie_break_history.db bos). "
            "Bu motor yalnizca gercek 95+ puanli beraberlikler olustugunda veri "
            "biriktirir - main.py'yi birkac hafta calistirdiktan sonra tekrar deneyin."
        )

    await _enrich_with_current_prices(rows)
    pairs = _build_pairs(rows)

    lines = [
        "=" * 50,
        "TIE-BREAK WEIGHT OPTIMIZER",
        "=" * 50,
        f"Toplam kayit: {len(rows)}",
        f"Karsilastirilabilir cift sayisi: {len(pairs)}",
        "",
    ]

    if len(pairs) < MIN_PAIRS:
        lines.append(
            f"YETERSIZ VERI: en az {MIN_PAIRS} cift gerekiyor, {len(pairs)} var. "
            "Hicbir agirlik degistirilmedi (asiri uydurmayi onlemek icin)."
        )
        return "\n".join(lines)

    current_weights = tbe.load_weights()
    current_score = concordance(pairs, current_weights)
    best_weights, best_score, tested = grid_search(pairs)

    lines.append(f"Grid search: {tested} kombinasyon denendi")
    lines.append(f"Mevcut agirliklarin concordance'i: {current_score:.3f}")
    lines.append(f"En iyi bulunan concordance: {best_score:.3f}")
    lines.append(f"Mevcut agirliklar: {current_weights}")
    lines.append(f"Onerilen agirliklar: {best_weights}")
    lines.append("")

    improvement = best_score - current_score
    if improvement < MIN_IMPROVEMENT:
        lines.append(
            f"Iyilesme yetersiz (+{improvement:.3f} < gerekli +{MIN_IMPROVEMENT}). "
            "Mevcut agirliklar korunuyor."
        )
        if apply:
            _log_entry(
                {
                    "tarih": datetime.now(timezone.utc).isoformat(),
                    "action": "no_change",
                    "sebep": f"iyilesme +{improvement:.3f}, esik +{MIN_IMPROVEMENT}",
                }
            )
        return "\n".join(lines)

    lines.append(f"Iyilesme yeterli (+{improvement:.3f}) - {'uygulaniyor' if apply else 'uygulanmadi (--apply kullanin)'}")
    if apply:
        apply_weights(best_weights, current_score, best_score, len(pairs))
        lines.append("config/tie_break_weights.json guncellendi (yedek: tie_break_weights_backup.json)")

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Documented future upgrade - NOT implemented, not claimed as such.
# ---------------------------------------------------------------------

class BayesianOptimizer:
    """Placeholder for a future Bayesian optimization upgrade path
    (e.g. Gaussian-process-based search via scikit-optimize). Not
    implemented in V1.1 - Grid Search above is the real, working
    optimizer. This class exists only so the upgrade path is visible
    in the code, not silently absent.
    """

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "Bayesian optimization is a documented future upgrade, not yet implemented. "
            "Use grid_search() / run_optimization() instead."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="QDE Tie-Break Weight Optimizer")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--report", action="store_true", help="Grid search yap, hicbir agirligi degistirme")
    group.add_argument("--apply", action="store_true", help="Grid search yap ve esikleri gecen sonucu uygula")
    group.add_argument("--rollback", action="store_true", help="Son degisikligi zorla geri al")
    args = parser.parse_args()

    if args.rollback:
        print("Agirliklar onceki yedege geri alindi." if rollback() else "Geri alinacak bir yedek bulunamadi.")
        return

    print(asyncio.run(run_optimization(apply=args.apply)))


if __name__ == "__main__":
    main()
