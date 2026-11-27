"""
QDE V1 - output/live_status.py

Live scan-state writer for the streaming dashboard (dashboard.html).
Purely presentational and additive: reads only what main.QDEPipeline
already produces per symbol (name, pass/fail, Result fields) and writes
a small JSON snapshot to disk. Does not touch indicators, engines, or
the setups database (Document 07 SS19 - Output Engine only presents).

The dashboard (a static HTML/JS file, opened in a browser and served
locally) polls this file every ~800ms instead of connecting to Python
directly - this keeps the dashboard fully decoupled from the engine
process, matching the same "read-only consumer" boundary every other
output/ module already respects.

Dependency level: 1 (depends on models.result).
"""

from __future__ import annotations

import json
import tempfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.result import Result

_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
_STATUS_FILE = _REPORTS_DIR / "live_status.json"

_TICKER_HISTORY = 40
_SIGNAL_FEED_SIZE = 15
_SCORE_HISTORY = 60


class LiveStatusWriter:
    """Accumulates rolling scan state and atomically writes it to
    reports/live_status.json after every symbol.

    Atomic write (write-to-temp-then-rename) so the dashboard never
    reads a half-written file mid-poll.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.completed = 0
        self.passed = 0
        self.rejected = 0
        self.current_symbol = ""
        self.ticker: deque[dict[str, Any]] = deque(maxlen=_TICKER_HISTORY)
        self.signal_feed: deque[dict[str, Any]] = deque(maxlen=_SIGNAL_FEED_SIZE)
        self.score_history: deque[float] = deque(maxlen=_SCORE_HISTORY)
        self._confidence_sum = 0.0
        self._score_sum = 0.0
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    def update(self, symbol: str, result: Result | None) -> None:
        self.completed += 1
        self.current_symbol = symbol
        passed = result is not None and result.setup.passed

        if passed:
            self.passed += 1
            self._confidence_sum += result.confidence
            self._score_sum += result.score
            self.score_history.append(result.score)
            self.signal_feed.appendleft(
                {
                    "symbol": result.symbol,
                    "confidence": result.confidence,
                    "score": result.score,
                    "risk_grade": result.risk_grade,
                    "uniqueness": result.uniqueness,
                }
            )
        else:
            self.rejected += 1
            if result is not None:
                self.score_history.append(result.score)

        self.ticker.append({"symbol": symbol, "passed": passed})
        self._write(scan_complete=False)

    def finish(self) -> None:
        self._write(scan_complete=True)

    def _write(self, scan_complete: bool) -> None:
        avg_confidence = round(self._confidence_sum / self.passed, 1) if self.passed else 0.0
        avg_score = round(self._score_sum / self.passed, 1) if self.passed else 0.0

        payload = {
            "current_symbol": self.current_symbol,
            "completed": self.completed,
            "total": self.total,
            "passed": self.passed,
            "rejected": self.rejected,
            "avg_confidence": avg_confidence,
            "avg_score": avg_score,
            "ticker": list(self.ticker),
            "signal_feed": list(self.signal_feed),
            "score_history": list(self.score_history),
            "scan_complete": scan_complete,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        # Atomic write: temp file in the same directory, then replace.
        fd, tmp_path = tempfile.mkstemp(dir=_REPORTS_DIR, suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            Path(tmp_path).replace(_STATUS_FILE)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
