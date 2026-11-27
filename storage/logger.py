"""
QDE V1 - storage/logger.py

Structured logging (Document 06 SS10). One shared configuration used
by every module in the project; nothing here depends on any other
QDE package.

V1.1 fix note: engines.research logs one INFO line per symbol
("Research: recorded ..."), which is correct for the log FILE (full
audit trail of every persisted setup, per Document 05 SS3) but floods
the console when scanning 500+ symbols. A console-only filter now
excludes that one logger's records from the terminal while leaving
the file handler - and every other logger - completely untouched.

Dependency level: 0 (no internal imports).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# Loggers whose records should still be written to the log file (full
# audit trail) but never printed to the console, because they emit one
# line per symbol during a scan of 500+ symbols (Document 07 SS3:
# terminal output must stay clean; database recording must not stop).
_CONSOLE_EXCLUDED_LOGGERS = {"qde.engines.research"}

# Minimum required log events per Document 06 SS10:
#   scan started, scan finished, API requests, API failures,
#   symbol skipped, indicator errors, hard filter rejection,
#   score calculation, confidence calculation, database write,
#   execution time.
# These are emitted by the relevant modules at INFO/WARNING/ERROR/DEBUG
# as appropriate; this module only configures the logging backend.


class _ExcludeLoggersFilter(logging.Filter):
    """Drops log records from specific logger names on one handler only.
    Used to keep the console clean without losing anything from the
    log file.
    """

    def __init__(self, excluded_names: set[str]) -> None:
        super().__init__()
        self._excluded_names = excluded_names

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name not in self._excluded_names


class QDELogger:
    """Factory for module-scoped loggers sharing one global configuration.

    Usage:
        logger = QDELogger.get(__name__)
        logger.info("Scan started")
    """

    @staticmethod
    def _configure(level: int = logging.INFO) -> None:
        global _CONFIGURED
        if _CONFIGURED:
            return

        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _LOG_DIR / "qde.log"

        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(_ExcludeLoggersFilter(_CONSOLE_EXCLUDED_LOGGERS))

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        # No filter here on purpose - the file keeps the complete record.

        root = logging.getLogger("qde")
        root.setLevel(level)
        root.addHandler(stream_handler)
        root.addHandler(file_handler)
        root.propagate = False

        _CONFIGURED = True

    @classmethod
    def get(cls, name: str, level: int = logging.INFO) -> logging.Logger:
        """Return a logger scoped under the shared 'qde' root logger."""
        cls._configure(level)
        qualified_name = name if name.startswith("qde") else f"qde.{name}"
        return logging.getLogger(qualified_name)

    @classmethod
    def set_level(cls, level: int) -> None:
        """Adjust verbosity at runtime (e.g. --debug CLI flag)."""
        cls._configure(level)
        logging.getLogger("qde").setLevel(level)
