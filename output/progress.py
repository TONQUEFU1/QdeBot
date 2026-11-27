"""
QDE V1 - output/progress.py

Terminal progress indicator (Document 07 SS5 UX intent: a professional
scan view, not a per-symbol log stream). Purely presentational - reads
only what main.QDEPipeline already produces per symbol (its name and
pass/fail status). Never touches indicators, engines, or storage
(Document 07 SS19).

V1.1 fix note: the original renderer padded every line to a fixed 90
characters. On a narrow terminal (e.g. a phone's Termux window, often
40-50 columns), a 90-character line wraps onto multiple screen rows,
and "\r" only returns the cursor to the start of the *last* wrapped
row - not the whole block - so each update stacked a new block below
the previous one instead of overwriting it (exactly the "coins
listing downward" symptom). This version:
    - Measures the real terminal width every render (shutil) and
      builds a line that always fits within it, so it can never wrap.
    - Uses the ANSI "clear to end of line" sequence instead of manual
      padding, so leftover characters from a longer previous line are
      always erased regardless of exact length.
    - Shortens ccxt-style symbols ("GPS/USDT:USDT") to the plain form
      ("GPSUSDT") for display only - shorter, and matches every other
      output surface (JSON/CSV/DB), which already stores symbols this
      way via the rest of the pipeline.

Dependency level: 0 (no internal imports beyond stdlib).
"""

from __future__ import annotations

import shutil
import sys

_CLEAR_TO_EOL = "\x1b[K"
_MIN_BAR_WIDTH = 8
_MAX_BAR_WIDTH = 24
_FALLBACK_COLUMNS = 60


def _short_symbol(symbol: str) -> str:
    """'GPS/USDT:USDT' -> 'GPSUSDT'; '1000SATS/USDT:USDT' -> '1000SATSUSDT'.
    Falls back to the original string unchanged if it doesn't match the
    ccxt swap-symbol shape (e.g. already plain, or unexpected format).
    """
    if "/" not in symbol:
        return symbol
    base = symbol.split("/", 1)[0]
    return f"{base}USDT"


class ProgressReporter:
    """Renders a single, continuously-updated progress line instead of
    one printed line per symbol. Call `update()` after each symbol
    completes; call `finish()` once, after the scan loop ends, before
    any further terminal output is printed.
    """

    def __init__(self, total: int, bar_width: int = 24, enabled: bool = True) -> None:
        self.total = max(total, 1)
        self.bar_width = bar_width
        self.enabled = enabled and sys.stdout.isatty()
        self.completed = 0
        self.passed = 0
        self.rejected = 0
        self._started = False

    def update(self, symbol: str, passed: bool) -> None:
        self.completed += 1
        if passed:
            self.passed += 1
        else:
            self.rejected += 1
        self._render(symbol)

    def _terminal_width(self) -> int:
        try:
            return shutil.get_terminal_size(fallback=(_FALLBACK_COLUMNS, 24)).columns
        except Exception:
            return _FALLBACK_COLUMNS

    def _render(self, current_symbol: str) -> None:
        if not self.enabled:
            return

        width = max(20, self._terminal_width() - 1)  # -1: avoid edge-of-line auto-wrap
        fraction = self.completed / self.total
        symbol_display = _short_symbol(current_symbol)

        # Fixed-cost part of the line (percentage + passed/rejected
        # counters); whatever width remains is split between the bar
        # and the symbol name, both shrinking gracefully on narrow
        # screens instead of ever exceeding `width`.
        suffix = f" {fraction * 100:5.1f}% P:{self.passed} R:{self.rejected}"
        budget_for_bar_and_symbol = max(10, width - len(suffix) - 3)  # -3 for "[ ] "
        bar_width = max(_MIN_BAR_WIDTH, min(_MAX_BAR_WIDTH, budget_for_bar_and_symbol // 2))
        symbol_budget = max(4, budget_for_bar_and_symbol - bar_width)
        symbol_display = symbol_display[:symbol_budget]

        filled = int(bar_width * fraction)
        bar = "█" * filled + "░" * (bar_width - filled)

        line = f"\r[{bar}]{suffix} {symbol_display}"
        line = line[:width]  # hard safety cap regardless of the budget math above

        sys.stdout.write(line + _CLEAR_TO_EOL)
        sys.stdout.flush()
        self._started = True

    def pause_for_message(self) -> None:
        """Move the cursor past the progress line so an interleaved
        message (e.g. a SIGNAL FOUND block) prints cleanly below it,
        without the next update overwriting it.
        """
        if self.enabled and self._started:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def finish(self) -> None:
        if self.enabled and self._started:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def print_header(self, total_symbols: int) -> None:
        print("-" * 54)
        print("QDE Quant Engine")
        print()
        print("Market: Binance Futures")
        print(f"Scanning {total_symbols} symbols...")
        print()
