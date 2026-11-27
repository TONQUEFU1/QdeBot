"""
QDE V1 - output/terminal.py

Terminal Output renderer (Document 07 SS5, SS8, SS9). Read-only
consumer of list[Result] - never downloads data, calculates
indicators, or alters scores/confidence (Document 07 SS19).

V1.1 changes:
    - `render()` (the end-of-scan summary) is now compact - full
      per-candidate detail is shown live via `print_signal_found()`
      the moment a symbol passes, so the final report doesn't repeat
      it (avoids the "wall of text" the per-candidate block produced
      when many symbols qualified).
    - `print_signal_found()` prints a colored block the instant a
      symbol passes the full pipeline, using ANSI escape codes. It
      only prints fields QDE actually computes (confidence, score,
      risk grade, momentum, relative strength, volume). Stop Loss /
      Take Profit / Risk-Reward / Entry are NOT included because no
      engine in this codebase computes them yet - printing them would
      mean fabricating numbers, which this renderer must never do
      (Document 07 SS19: it only presents what was actually
      calculated).

Dependency level: 2 (depends on models.result, storage.exporter).
"""

from __future__ import annotations

from typing import Any

from models.result import Result
from storage.exporter import to_explainability_dict

_TOP_SIGNALS_DISPLAYED = 15
_SEPARATOR = "=" * 54

# ANSI color codes. Terminal renderers that redirect to a non-tty
# (e.g. piped into a file) will see raw escape codes; callers that
# care can check sys.stdout.isatty() before invoking these functions,
# matching output.progress.ProgressReporter's own tty check.
_GREEN = "\033[92m"
_ORANGE = "\033[93m"
_TURQUOISE = "\033[96m"
_PURPLE = "\033[95m"
_YELLOW = "\033[33m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _color(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}"


def _category(result: Result) -> str:
    """Research category label instead of BUY/SELL (Document 07 SS4)."""
    if not result.setup.passed:
        return "Rejected"
    if result.is_institutional_grade():
        return "Institutional Grade"
    if result.confidence >= 85 and result.score >= 80:
        return "High Probability"
    if result.confidence >= 70:
        return "Momentum Candidate"
    if result.score >= 60:
        return "Early Expansion"
    if result.score >= 40:
        return "Watchlist"
    return "Speculative"


# Public alias: performance_tracker.py (a standalone plugin module)
# reuses this categorization for display consistency. No behavior
# change - same function, just importable under a non-private name.
category_label = _category


def print_signal_found(result: Result) -> None:
    """Print a colored block the instant a symbol passes the full
    pipeline (Hard Filter + Scoring + Confidence). Only fields QDE
    actually computed are shown.
    """
    fs = result.setup.feature_scores
    print()
    print(_color("✓ SIGNAL FOUND", _BOLD + _GREEN))
    print()
    print(_color(result.symbol, _BOLD))
    print(f"Category ........... {_category(result)}")
    print(f"Confidence .......... {_color(f'{result.confidence:.0f}', _GREEN)}")
    print(f"Score ............... {_color(f'{result.score:.0f}', _GREEN)}")
    print(f"Grade ............... {_color(result.risk_grade, _GREEN)}")
    if "momentum" in fs:
        momentum_str = f"{fs['momentum']:.1f}"
        print(f"Momentum ............ {_color(momentum_str, _GREEN)}")
    if "relative_strength" in fs:
        rs_str = f"{fs['relative_strength']:.1f}"
        print(f"Relative Strength ... {_color(rs_str, _GREEN)}")
    if "volume" in fs:
        volume_str = f"{fs['volume']:.1f}"
        print(f"Volume Score ........ {_color(volume_str, _GREEN)}")
    print(f"Uniqueness .......... {result.uniqueness:.0f}")
    print(f"Expected Window ..... {result.expected_duration}")
    if result.contradictions:
        print(f"Warnings ............ {_color(', '.join(result.contradictions), _ORANGE)}")
    # Stop Loss / Take Profit / Risk-Reward / Entry intentionally
    # omitted: no engine in this codebase computes them (see module
    # docstring). They will appear here, in these colors, once a V2
    # risk-management engine actually produces them:
    #   Stop Loss    -> orange   Take Profit -> turquoise
    #   Risk/Reward  -> purple   Entry       -> yellow
    print("-" * 54)


def _print_top_signals(results: list[Result], tie_break_results: dict[str, Any] | None = None) -> None:
    passed = [r for r in results if r.setup.passed]
    top = passed[:_TOP_SIGNALS_DISPLAYED]
    if not top:
        print("Top Signals: none")
        return

    print("Top Signals")
    print()
    for i, result in enumerate(top, start=1):
        print(f"{i})")
        print(result.symbol)
        print(f"Confidence {result.confidence:.0f}")
        print(f"Score      {result.score:.0f}")
        if tie_break_results and result.symbol in tie_break_results:
            tb = tie_break_results[result.symbol]
            print(f"TieBreak   {tb.total:+.1f}")
        print()


def render(results: list[Result], scan_summary: dict[str, Any], tie_break_results: dict[str, Any] | None = None) -> None:
    """Print the compact end-of-scan report (Document 07 SS5, SS8).

    Rich per-candidate detail (positive/negative factors, expected
    move, etc.) was already printed live via print_signal_found() as
    each symbol passed - this summary intentionally does not repeat
    it, to keep the final report scannable even with many signals.

    `tie_break_results`, if provided (main.QDEPipeline.tie_break_results),
    shows the additive TieBreak delta next to any symbol the Tie-Break
    Engine actually reordered (tie_break_engine.py) - never replaces
    Score/Confidence, only supplements them (Document 12's own
    explainability requirement).
    """
    passed = [r for r in results if r.setup.passed]
    institutional = [r for r in passed if r.is_institutional_grade()]

    print()
    print(_SEPARATOR)
    print("QDE Scan Report")
    print(_SEPARATOR)
    print()
    print(f"Coins Scanned      : {scan_summary.get('symbols_scanned', 0)}")
    print(f"Qualified          : {scan_summary.get('symbols_passed', 0)}")
    print(f"Rejected           : {scan_summary.get('symbols_scanned', 0) - scan_summary.get('symbols_passed', 0)}")
    print(f"Institutional Grade: {len(institutional)}")
    print(f"Average Score      : {scan_summary.get('avg_score', 0.0)}")
    print(f"Average Confidence : {scan_summary.get('avg_confidence', 0.0)}")
    print(f"Scan Duration      : {scan_summary.get('scan_duration_seconds', 0)} sec")
    print()
    print("-" * 54)
    print()

    _print_top_signals(results, tie_break_results)

    print(_SEPARATOR)
