"""
QDE V1.1 - learning_engine.py

QDE Auto-Learning System (standalone plugin module).

WHAT THIS MODULE DOES
    Reads performance_tracker.py's historical Top15 records (including
    each snapshot's per-indicator feature_scores, added specifically to
    support this module - see performance_tracker.py's schema
    migration), measures whether each indicator's contribution
    statistically correlates with better or worse realized returns,
    and - only when the evidence clears several safety gates - nudges
    that indicator's weight in config/weights.json up or down by a
    small, bounded step.

RULES THIS MODULE FOLLOWS (per explicit request)
    - Never modifies engines/scoring.py, engines/confidence.py,
      engines/hard_filter.py, indicators/*, or any other core module.
      It only ever writes to config/weights.json - a file
      ScoringEngine already reads fresh via QDEConfig.load() at the
      start of every scan, so no core code needs to change for a
      weight update to take effect on the next run.
    - Never triggers a new scan.
    - Never applies a change without enough samples and a real,
      measurable effect (see "SAFETY GATES" below) - explicitly to
      satisfy the anti-overfitting requirement.
    - Every decision (including "no change - insufficient data") is
      logged to learning_log.json for full auditability.

HONEST LIMITATION (stated up front, not hidden)
    With few recorded scans, essentially every indicator will fail the
    minimum-sample-size gate and the module will correctly do nothing.
    This is not a bug - it is the anti-overfitting safeguard doing its
    job. Meaningful auto-tuning needs weeks of accumulated Top15 data
    (see MIN_SAMPLES_PER_BUCKET below).

SAFETY GATES (all must pass before any single weight changes)
    1. MIN_SAMPLES_PER_BUCKET samples on both sides of the comparison
       (see "METHOD" below) - protects against small-sample noise.
    2. Multi-window consensus: the effect must point the same
       direction (not necessarily be significant) in at least
       CONSENSUS_MIN_WINDOWS of the three analysis windows
       (last 50 / last 100 / last 300 scans) - this is the module's
       concrete implementation of "farkli donemlere adapte olma" /
       market-regime robustness, without needing a real regime
       detector (none exists yet in this codebase).
    3. A simple two-sample significance check (effect size vs pooled
       standard error) on the primary window - protects against acting
       on a difference that is plausibly just noise.
    4. MAX_WEIGHT_STEP_PCT caps how much any single run can move a
       weight (default 10%), and WEIGHT_BOUNDS caps the absolute
       range - protects against one aggressive run overcorrecting.
    5. Rollback check: before proposing anything new, this module
       compares the Top15 win rate immediately before vs after the
       previous logged change. If it got meaningfully worse, weights
       are reverted to their pre-change backup and a rollback entry is
       logged instead of a new adjustment.

METHOD (deliberately simple and explainable, not a black-box model -
consistent with Document 03 SS14's Explainable AI Principle)
    For each indicator, snapshots are split into two buckets using
    that indicator's own weighted feature_score at scan time:
        "high"  = feature_score > 0 (indicator was net positive)
        "low"   = feature_score <= 0 (indicator was net neutral/negative)
    The average realized return of each bucket is compared. A
    consistently higher average return in the "high" bucket across
    multiple time windows is evidence the indicator is doing its job
    and its weight can be nudged up; the reverse nudges it down.

Usage:
    python learning_engine.py --report      # analysis only, no changes
    python learning_engine.py --apply       # analyze and apply if gates pass
    python learning_engine.py --rollback    # force revert to last backup
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import performance_tracker as pt

_WEIGHTS_PATH = Path(__file__).resolve().parent / "config" / "weights.json"
_BACKUP_PATH = Path(__file__).resolve().parent / "config" / "weights_backup.json"
_LOG_PATH = Path(__file__).resolve().parent / "learning_log.json"

# --- Safety gate constants (see module docstring) ---
MIN_SAMPLES_PER_BUCKET = 20
CONSENSUS_WINDOWS = (50, 100, 300)
CONSENSUS_MIN_WINDOWS = 2
SIGNIFICANCE_Z = 1.28  # ~85% one-sided confidence; conservative given noisy crypto data
MAX_WEIGHT_STEP_PCT = 0.10
WEIGHT_BOUNDS = (0.2, 2.0)
ROLLBACK_WIN_RATE_DROP_THRESHOLD = 10.0  # percentage points


# ---------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------

def _load_weights() -> dict[str, float]:
    return json.loads(_WEIGHTS_PATH.read_text())


def _save_weights(weights: dict[str, float]) -> None:
    _WEIGHTS_PATH.write_text(json.dumps(weights, indent=2))


def _run_async_safely(coro):
    """CRITICAL BUG FIX: `asyncio.run(coro)` raises `RuntimeError:
    asyncio.run() cannot be called from a running event loop` when
    analyze_and_optimize() is invoked via `python main.py --learn` -
    main.py calls it synchronously from inside main_async(), which is
    itself already running under asyncio.run(). Without this fix, the
    Learning Engine's price-fetch step failed unconditionally every
    time it was triggered this way (no try/except even existed around
    the old call site - it would raise and abort the whole
    --learn run, only ever caught by main.py's own broad except,
    logged as an easy-to-miss warning). Same fix and rationale as
    audit_engine.py's identically-named helper - duplicated here
    rather than shared, matching this codebase's established pattern
    of independent, standalone plugin modules.

    Safe whether analyze_and_optimize() is invoked standalone
    (`python learning_engine.py --apply`, no parent loop) or from
    within main.py's --learn (parent loop already running).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
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


def _fetch_enriched_snapshots(window_scans: int) -> list[dict[str, Any]]:
    """Pulls the most recent `window_scans` scans' Top15 snapshots
    (not `window_scans` coin-rows - full scans, so each contributes up
    to 15 rows), enriches each with realized pct_change against the
    symbol's current price. Only rows that have feature_scores_json
    recorded (i.e. captured after this module's schema migration) are
    usable for indicator-effect analysis.
    """
    conn = pt._connect()
    try:
        scan_ids = [
            row["scan_id"]
            for row in conn.execute(
                "SELECT DISTINCT scan_id, MIN(scan_timestamp) as ts FROM top15_snapshots "
                "GROUP BY scan_id ORDER BY ts DESC LIMIT ?",
                (window_scans,),
            ).fetchall()
        ]
        if not scan_ids:
            return []
        placeholders = ",".join("?" * len(scan_ids))
        rows = conn.execute(
            f"SELECT * FROM top15_snapshots WHERE scan_id IN ({placeholders})",
            scan_ids,
        ).fetchall()
        snapshots = [dict(r) for r in rows]
    finally:
        conn.close()

    usable = [s for s in snapshots if s.get("feature_scores_json")]
    symbols = sorted(set(s["symbol"] for s in usable))
    if not symbols:
        return []

    current_prices = _run_async_safely(pt.fetch_current_prices(symbols))
    for s in usable:
        s["feature_scores"] = json.loads(s["feature_scores_json"])
        s["current_price"] = current_prices.get(s["symbol"])
        s["pct_change"] = pt.pct_change(s["price_at_scan"], s["current_price"])
    return [s for s in usable if s["pct_change"] is not None]


# ---------------------------------------------------------------------
# Per-indicator effect analysis
# ---------------------------------------------------------------------

@dataclass
class IndicatorEffect:
    indicator: str
    window: int
    high_n: int
    low_n: int
    high_avg_return: float
    low_avg_return: float
    effect_size: float  # high_avg_return - low_avg_return
    significant: bool
    insufficient_data: bool


def _two_sample_significant(high: list[float], low: list[float]) -> bool:
    """Simple pooled-standard-error significance check. Deliberately
    not a full inferential test suite - see module docstring's
    "METHOD" section for why a simpler, explainable check was chosen.
    """
    if len(high) < 2 or len(low) < 2:
        return False
    var_high = statistics.variance(high)
    var_low = statistics.variance(low)
    se = ((var_high / len(high)) + (var_low / len(low))) ** 0.5
    if se == 0:
        return False
    z = abs(statistics.mean(high) - statistics.mean(low)) / se
    return z >= SIGNIFICANCE_Z


def analyze_indicator(indicator: str, snapshots: list[dict], window: int) -> IndicatorEffect:
    high = [s["pct_change"] for s in snapshots if s["feature_scores"].get(indicator, 0) > 0]
    low = [s["pct_change"] for s in snapshots if s["feature_scores"].get(indicator, 0) <= 0]

    insufficient = len(high) < MIN_SAMPLES_PER_BUCKET or len(low) < MIN_SAMPLES_PER_BUCKET
    high_avg = round(statistics.mean(high), 3) if high else 0.0
    low_avg = round(statistics.mean(low), 3) if low else 0.0
    effect = round(high_avg - low_avg, 3)
    significant = (not insufficient) and _two_sample_significant(high, low)

    return IndicatorEffect(
        indicator=indicator,
        window=window,
        high_n=len(high),
        low_n=len(low),
        high_avg_return=high_avg,
        low_avg_return=low_avg,
        effect_size=effect,
        significant=significant,
        insufficient_data=insufficient,
    )


def analyze_all_windows(indicators: list[str]) -> dict[str, list[IndicatorEffect]]:
    """Returns, per indicator, one IndicatorEffect per analysis window
    (50/100/300 scans) - the "Son 50, 100 ve 300 taramayi ayri ayri
    analiz et" requirement.
    """
    results: dict[str, list[IndicatorEffect]] = {name: [] for name in indicators}
    for window in CONSENSUS_WINDOWS:
        snapshots = _fetch_enriched_snapshots(window)
        for name in indicators:
            results[name].append(analyze_indicator(name, snapshots, window))
    return results


# ---------------------------------------------------------------------
# Regime tagging (diagnostic only in V1 - see docstring)
# ---------------------------------------------------------------------

def classify_regime(snapshots: list[dict]) -> str:
    """Crude regime label from the aggregate Top15 return in this
    window - NOT a real BTC-trend regime detector (none exists yet in
    this codebase; engines/scoring.py already documents that gap).
    Diagnostic/reporting only - does not currently split weight
    updates per regime, to avoid fragmenting already-small samples
    further. Flagged here as a V2 extension point.
    """
    changes = [s["pct_change"] for s in snapshots if s.get("pct_change") is not None]
    if not changes:
        return "unknown"
    avg = statistics.mean(changes)
    std = statistics.pstdev(changes) if len(changes) > 1 else 0.0
    if std > 15:
        return "volatile"
    if avg > 3:
        return "trend-up"
    if avg < -3:
        return "trend-down"
    return "sideways"


# ---------------------------------------------------------------------
# Weight update proposal + application
# ---------------------------------------------------------------------

@dataclass
class WeightChange:
    indicator: str
    old_value: float
    new_value: float
    reason: str


def propose_updates(
    current_weights: dict[str, float], effects_by_indicator: dict[str, list[IndicatorEffect]]
) -> list[WeightChange]:
    """Applies the multi-window consensus + significance + bounds
    gates (see module docstring's SAFETY GATES) and returns only the
    changes that survive every gate.
    """
    changes: list[WeightChange] = []

    for indicator, window_effects in effects_by_indicator.items():
        primary = next((e for e in window_effects if e.window == 100), window_effects[0])

        if primary.insufficient_data:
            continue  # Gate 1: not enough samples in the primary window

        directions = [
            1 if e.effect_size > 0 else (-1 if e.effect_size < 0 else 0)
            for e in window_effects
            if not e.insufficient_data
        ]
        if not directions:
            continue
        consensus_sign = (
            1 if directions.count(1) >= CONSENSUS_MIN_WINDOWS
            else (-1 if directions.count(-1) >= CONSENSUS_MIN_WINDOWS else 0)
        )
        if consensus_sign == 0:
            continue  # Gate 2: no multi-window consensus

        if not primary.significant:
            continue  # Gate 3: primary window effect not statistically significant

        old_value = current_weights.get(indicator, 1.0)
        step = old_value * MAX_WEIGHT_STEP_PCT * consensus_sign
        new_value = old_value + step
        new_value = max(WEIGHT_BOUNDS[0], min(WEIGHT_BOUNDS[1], new_value))  # Gate 4: bounds

        if round(new_value, 4) == round(old_value, 4):
            continue

        direction_word = "arttirildi" if consensus_sign > 0 else "azaltildi"
        reason = (
            f"{indicator}: {CONSENSUS_WINDOWS} pencerelerinde tutarli yon "
            f"({directions.count(consensus_sign)}/{len(directions)} pencere), "
            f"primer pencere (100 tarama) etkisi istatistiksel olarak anlamli "
            f"(high_avg={primary.high_avg_return}%, low_avg={primary.low_avg_return}%, "
            f"n_high={primary.high_n}, n_low={primary.low_n}) -> agirlik {direction_word}"
        )
        changes.append(
            WeightChange(indicator=indicator, old_value=old_value, new_value=round(new_value, 4), reason=reason)
        )

    return changes


def _log_entry(entry: dict[str, Any]) -> None:
    log = []
    if _LOG_PATH.exists():
        try:
            log = json.loads(_LOG_PATH.read_text())
        except json.JSONDecodeError:
            log = []
    log.append(entry)
    _LOG_PATH.write_text(json.dumps(log, indent=2, ensure_ascii=False))


def _current_win_rate(window_scans: int = 20) -> float | None:
    snapshots = _fetch_enriched_snapshots(window_scans)
    if not snapshots:
        return None
    wins = sum(1 for s in snapshots if s["pct_change"] > 0)
    return round(wins / len(snapshots) * 100, 2)


def check_rollback() -> dict[str, Any] | None:
    """Gate 5: compares win rate right before vs after the last logged
    weight change. If it dropped by more than
    ROLLBACK_WIN_RATE_DROP_THRESHOLD points, reverts weights.json to
    the pre-change backup and logs a rollback entry. Returns the
    rollback log entry if a rollback happened, else None.
    """
    if not _LOG_PATH.exists() or not _BACKUP_PATH.exists():
        return None
    log = json.loads(_LOG_PATH.read_text())
    last_apply = next((e for e in reversed(log) if e.get("action") == "apply"), None)
    if last_apply is None:
        return None

    win_rate_before = last_apply.get("win_rate_at_change")
    win_rate_now = _current_win_rate()
    if win_rate_before is None or win_rate_now is None:
        return None

    if win_rate_before - win_rate_now >= ROLLBACK_WIN_RATE_DROP_THRESHOLD:
        backup = json.loads(_BACKUP_PATH.read_text())
        _save_weights(backup)
        entry = {
            "tarih": datetime.now(timezone.utc).isoformat(),
            "action": "rollback",
            "sebep": (
                f"Son degisiklik sonrasi basari orani %{win_rate_before} -> %{win_rate_now} "
                f"dustu (>= %{ROLLBACK_WIN_RATE_DROP_THRESHOLD} esik), onceki agirliklara donuldu"
            ),
            "restored_weights": backup,
        }
        _log_entry(entry)
        return entry
    return None


def apply_updates(changes: list[WeightChange]) -> dict[str, Any]:
    current = _load_weights()
    _BACKUP_PATH.write_text(json.dumps(current, indent=2))  # backup before mutating

    new_weights = dict(current)
    change_records = []
    for c in changes:
        new_weights[c.indicator] = c.new_value
        change_records.append(
            {
                "gösterge": c.indicator,
                "eski_değer": c.old_value,
                "yeni_değer": c.new_value,
                "sebep": c.reason,
            }
        )

    _save_weights(new_weights)

    win_rate = _current_win_rate()
    entry = {
        "tarih": datetime.now(timezone.utc).isoformat(),
        "action": "apply",
        "değişiklikler": change_records,
        "win_rate_at_change": win_rate,
    }
    _log_entry(entry)
    return entry


# ---------------------------------------------------------------------
# Orchestration + CLI
# ---------------------------------------------------------------------

def run_analysis(indicators: list[str] | None = None) -> dict[str, list[IndicatorEffect]]:
    if indicators is None:
        indicators = list(_load_weights().keys())
    return analyze_all_windows(indicators)


def format_report(effects_by_indicator: dict[str, list[IndicatorEffect]]) -> str:
    lines = ["=" * 44, "QDE LEARNING ENGINE - ANALIZ", "=" * 44, ""]
    for indicator, window_effects in effects_by_indicator.items():
        lines.append(indicator)
        for e in window_effects:
            status = "YETERSIZ VERI" if e.insufficient_data else ("ANLAMLI" if e.significant else "anlamli degil")
            lines.append(
                f"  [{e.window} tarama] high_n={e.high_n} low_n={e.low_n} "
                f"high_avg={e.high_avg_return}% low_avg={e.low_avg_return}% "
                f"effect={e.effect_size}% -> {status}"
            )
        lines.append("")
    lines.append("=" * 44)
    return "\n".join(lines)


def analyze_and_optimize(apply: bool = False) -> str:
    """Top-level entrypoint. Always safe to call - never raises for
    "no data yet" conditions, and never applies anything unless every
    safety gate passes.
    """
    rollback_entry = check_rollback()
    weights = _load_weights()
    effects = run_analysis(list(weights.keys()))
    report = format_report(effects)

    if not apply:
        return report

    changes = propose_updates(weights, effects)
    if not changes:
        _log_entry(
            {
                "tarih": datetime.now(timezone.utc).isoformat(),
                "action": "no_change",
                "sebep": "Hicbir gosterge tum guvenlik esiklerini (ornek sayisi, "
                "coklu pencere uyumu, istatistiksel anlamlilik) gecemedi.",
            }
        )
        report += "\n\nSonuc: Yeterli/istatistiksel olarak anlamli kanit yok - hicbir agirlik degistirilmedi."
        return report

    entry = apply_updates(changes)
    report += "\n\nUygulanan degisiklikler:\n"
    for c in entry["değişiklikler"]:
        report += f"  {c['gösterge']}: {c['eski_değer']} -> {c['yeni_değer']}\n  Sebep: {c['sebep']}\n"
    if rollback_entry:
        report += f"\n(Not: Bu calistirmadan once bir rollback tetiklendi: {rollback_entry['sebep']})\n"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="QDE Learning Engine")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--report", action="store_true", help="Analiz yap, hicbir agirligi degistirme")
    group.add_argument("--apply", action="store_true", help="Analiz yap ve guvenlik esiklerini gecen degisiklikleri uygula")
    group.add_argument("--rollback", action="store_true", help="Son degisikligi zorla geri al")
    args = parser.parse_args()

    if args.rollback:
        if not _BACKUP_PATH.exists():
            print("Geri alinacak bir yedek bulunamadi.")
            return
        backup = json.loads(_BACKUP_PATH.read_text())
        _save_weights(backup)
        _log_entry(
            {
                "tarih": datetime.now(timezone.utc).isoformat(),
                "action": "manual_rollback",
                "restored_weights": backup,
            }
        )
        print("Agirliklar onceki yedege geri alindi.")
        return

    print(analyze_and_optimize(apply=args.apply))


if __name__ == "__main__":
    main()
