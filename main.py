"""
QDE V1 - Quant Decision Engine
main.py

Orchestration entrypoint. This module contains NO business logic of its
own. Its sole responsibility is to wire together the fixed nine-stage
pipeline defined in Document 06 SS8 and execute one full scan.

Architecture contract (frozen - see Document 09 SS11 addendum):
    - Database access goes through the SetupRepository abstraction
      (storage.repository), never through a concrete DB driver directly.
    - Uniqueness scoring goes through the UniquenessProvider abstraction
      (engines.uniqueness), injected into ConfidenceEngine.
    - Indicators are discovered via IndicatorRegistry
      (indicators.registry); ScoringEngine never imports a concrete
      indicator module by name.

Pipeline order (must not be reordered or skipped):
    1. Load configuration
    2. Initialize infrastructure (logger, repository, cache, exchange client)
    3. Discover active symbol universe
    4. Per-symbol: collect -> hard filter -> score -> confidence -> record
    5. Aggregate results
    6. Rank results
    7. Render terminal report
    8. Write JSON output
    9. Write CSV output
    10. Log scan summary

NOTE: This file imports modules that will be generated in subsequent
steps (data.*, indicators.*, engines.*, storage.*, output.*). It defines
the interface contract those modules must implement; it is not runnable
until they exist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data.exchange import ExchangeClient
from data.collector import DataCollector
from data.cache import MarketCache

from indicators.registry import IndicatorRegistry

from engines.hard_filter import HardFilterEngine
from engines.scoring import ScoringEngine
from engines.confidence import ConfidenceEngine
from engines.uniqueness import BatchUniquenessProvider
from engines.research import ResearchEngine

from storage.logger import QDELogger
from storage.sqlite_repository import SQLiteRepository

from output.terminal import render as render_terminal, print_signal_found
from output.json_output import render as render_json
from output.csv_output import render as render_csv
from output.progress import ProgressReporter
from output.live_status import LiveStatusWriter
from tie_break_engine import TieBreakEngine, TieBreakInputs, build_inputs, apply_tie_break, record_tie_break_results
from market_regime import classify_regime, REGIME_UNKNOWN

from models.result import Result


CONFIG_DIR = Path(__file__).parent / "config"


@dataclass
class QDEConfig:
    """Typed container for the three configuration files.

    Loaded once at startup and passed by reference through the pipeline.
    No module re-reads these files mid-scan (Document 06 SS5).
    """

    general: dict[str, Any]
    filters: dict[str, Any]
    weights: dict[str, Any]

    @classmethod
    def load(cls, config_dir: Path = CONFIG_DIR) -> "QDEConfig":
        """Load config.json, filters.json and weights.json from disk."""
        general = json.loads((config_dir / "config.json").read_text())
        filters = json.loads((config_dir / "filters.json").read_text())
        weights = json.loads((config_dir / "weights.json").read_text())
        return cls(general=general, filters=filters, weights=weights)

    @property
    def version(self) -> str:
        """Deep Audit patch item #9: a deterministic identifier of
        this exact config/filters/weights content, so every setup
        recorded under it can later be traced back to "which
        parameters produced this score". A short SHA-256 hash of the
        three files' canonicalized JSON content - identical content
        always yields the identical version string; any change to any
        of the three files (including a single weight) yields a
        different one. Not a manually-incremented number, so it can
        never be forgotten to bump.
        """
        import hashlib

        canonical = json.dumps(
            {"general": self.general, "filters": self.filters, "weights": self.weights},
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


@dataclass
class ScanSummary:
    """Aggregate statistics for a single completed scan run."""

    run_id: str
    started_at: float
    finished_at: float
    symbols_scanned: int
    symbols_passed: int
    avg_confidence: float
    avg_score: float

    @property
    def duration_seconds(self) -> float:
        return round(self.finished_at - self.started_at, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "symbols_scanned": self.symbols_scanned,
            "symbols_passed": self.symbols_passed,
            "avg_confidence": self.avg_confidence,
            "avg_score": self.avg_score,
            "scan_duration_seconds": self.duration_seconds,
        }


class QDEPipeline:
    """Wires all layers together and executes one full scan.

    This class performs orchestration only. Every real decision (filter,
    score, confidence, storage) is delegated to the appropriate engine.
    """

    def __init__(
        self,
        config: QDEConfig,
        show_rejections: bool = False,
        live_signals: bool = False,
        audit_capture: bool = False,
    ) -> None:
        self.config = config
        self.show_rejections = show_rejections
        # V1.1 fix: off by default - see run()'s scan loop for why
        # (live per-symbol printing used to flood the terminal with
        # every Hard-Filter-passed symbol, not just real signals).
        self.live_signals = live_signals
        # AI Audit Engine capture (see audit_engine.py module
        # docstring). Off by default - only records the extra
        # per-indicator/per-rejection detail needed for a full audit
        # package when explicitly requested.
        self.audit_capture = audit_capture
        self._audit_scan_id: str | None = None
        # Tie-Break Ranking Engine (see tie_break_engine.py module
        # docstring). Only re-orders symbols already tied at the top
        # score band - never changes raw_score/confidence. Inputs are
        # collected cheaply per symbol during _process_one and
        # discarded after each scan's ranking.
        self.tie_break_engine = TieBreakEngine()
        # Deep Audit patch item #9: computed once, from the exact
        # config the pipeline was constructed with - never recomputed
        # mid-scan (Document 06 SS5: config is loaded once, not
        # re-read).
        self._config_version = config.version
        self._tie_break_inputs: dict[str, TieBreakInputs] = {}
        # Market Regime (see market_regime.py). Computed once per scan
        # from BTCUSDT before the main per-symbol loop starts, then
        # held constant for the whole scan - Document 04 SS12 treats
        # regime as a property of "the market right now", not
        # something that changes symbol-to-symbol.
        self._market_regime: str = REGIME_UNKNOWN
        self.tie_break_results: dict[str, Any] = {}
        self.logger = QDELogger.get(__name__)

        # Infrastructure
        self.repository = SQLiteRepository(
            db_path=config.general.get("database_path", "qde.db")
        )
        self.cache = MarketCache(ttl_config=config.general["cache"]["ttl_seconds"])
        self.exchange = ExchangeClient(
            rate_limit_config=config.general["rate_limit"]
        )
        self.collector = DataCollector(
            exchange=self.exchange,
            cache=self.cache,
            timeframes=config.general["timeframes"],
            required_candles=config.general["required_candles"],
        )

        # Indicator discovery (plugin/registry architecture - frozen decision)
        self.indicator_registry = IndicatorRegistry.discover()

        # Engines
        self.hard_filter = HardFilterEngine(filters=config.filters)
        self.scoring_engine = ScoringEngine(
            registry=self.indicator_registry,
            weights=config.weights,
        )
        # V1 uniqueness provider operates on the current batch only.
        # Swapping to a historical provider later requires no change to
        # ConfidenceEngine's public API (Document 09 SS11 addendum).
        self.uniqueness_provider = BatchUniquenessProvider()
        self.confidence_engine = ConfidenceEngine(
            uniqueness_provider=self.uniqueness_provider
        )
        self.research_engine = ResearchEngine(repository=self.repository)

        self.max_parallel = config.general["concurrency"]["max_parallel_symbols"]

    async def run(self) -> tuple[list[Result], ScanSummary]:
        """Execute one full scan and return ranked results + summary."""
        run_id = str(uuid.uuid4())
        self._audit_scan_id = run_id
        started_at = time.time()
        self.logger.info(f"Scan started (run_id={run_id})")

        symbols = await self.exchange.get_active_symbols()
        self.logger.info(f"Symbol universe discovered: {len(symbols)} symbols")

        # Bulk API prefetch (Document "Bulk API" revision) - warms the
        # ticker/funding/mark_price cache in 3 calls instead of up to
        # ~1,500 individual per-symbol calls (Document 10 SS6.2).
        await self.collector.prefetch_bulk(symbols)

        # Market Regime (see market_regime.py) - computed once, here,
        # from BTC's own indicators, before any symbol is scored.
        self._market_regime = await self._detect_market_regime(symbols)
        self.logger.info(f"Market regime detected: {self._market_regime}")

        progress = ProgressReporter(total=len(symbols))
        progress.print_header(len(symbols))
        live_status = LiveStatusWriter(total=len(symbols))

        semaphore = asyncio.Semaphore(self.max_parallel)
        results: list[Result] = []

        async def process_symbol(symbol: str) -> tuple[str, Result | None]:
            async with semaphore:
                result = await self._process_one(symbol)
                return symbol, result

        tasks = [process_symbol(s) for s in symbols]
        for coro in asyncio.as_completed(tasks):
            try:
                symbol, result = await coro
            except Exception as exc:  # noqa: BLE001 - isolate per-symbol failures
                self.logger.error(f"Unhandled symbol error: {exc}")
                continue

            symbol_passed = result is not None and result.setup.passed
            progress.update(symbol, symbol_passed)
            live_status.update(symbol, result)

            if result is not None:
                results.append(result)
                # V1.1 fix: do NOT live-print every Hard-Filter-passed
                # symbol during the scan. Hard Filter passing means
                # "data quality was good enough to score" (Document 02
                # SS15) - it says nothing about signal quality, so on
                # a typical scan 100+ symbols can pass it and each one
                # used to print a full colored block, flooding the
                # terminal. Only the progress bar updates live now;
                # the Top 15 are shown once, in the compact report,
                # after the scan finishes. Opt back into the old
                # per-symbol live printing with --live-signals.
                if symbol_passed and self.live_signals:
                    progress.pause_for_message()
                    print_signal_found(result)

        progress.finish()
        live_status.finish()

        ranked = await self._rank(results)

        finished_at = time.time()
        passed = [r for r in ranked if r.setup.passed]
        summary = ScanSummary(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            symbols_scanned=len(symbols),
            symbols_passed=len(passed),
            avg_confidence=self._avg([r.confidence for r in passed]),
            avg_score=self._avg([r.setup.raw_score for r in passed]),
        )
        self.logger.info(
            f"Scan finished (run_id={run_id}) in {summary.duration_seconds}s "
            f"- {summary.symbols_passed}/{summary.symbols_scanned} passed"
        )
        return ranked, summary

    async def _detect_market_regime(self, symbols: list[str]) -> str:
        """Fetches BTC's MarketData and runs it through the same
        indicator registry every other symbol uses, then classifies
        regime from the result (market_regime.classify_regime()).

        BTC's symbol string format depends on what the exchange
        returned in `symbols` (e.g. ccxt's unified "BTC/USDT:USDT") -
        found by prefix match rather than a hardcoded literal, so this
        doesn't break if the exchange layer's symbol format changes.

        Never raises: any failure (BTC not in the scanned universe,
        data fetch error, insufficient candle history) results in
        REGIME_UNKNOWN, which both ScoringEngine and ConfidenceEngine
        already handle as a neutral no-adjustment case - an honest
        "couldn't determine" rather than a guess.
        """
        btc_symbol = next((s for s in symbols if s.upper().startswith("BTC/") or s.upper() == "BTCUSDT"), None)
        if btc_symbol is None:
            self.logger.warning("BTC symbol not found in scanned universe - market regime will be 'unknown'")
            return REGIME_UNKNOWN

        try:
            btc_market_data = await self.collector.fetch(btc_symbol)
            btc_features = [indicator.calculate(btc_market_data) for indicator in self.indicator_registry.get_all()]
            regime = classify_regime(btc_features)

            # V1 Final Audit revision - "Data Collection Readiness":
            # persist the raw BTC values that produced this label, not
            # just the final string, so a future retuning of
            # market_regime.py's threshold can be evaluated against
            # real historical data instead of only future scans.
            try:
                by_name = {f.name: f for f in btc_features}
                trend_meta = by_name.get("trend").metadata if by_name.get("trend") else {}
                vol_meta = by_name.get("volatility").metadata if by_name.get("volatility") else {}
                await asyncio.to_thread(
                    self.repository.record_regime_snapshot,
                    scan_id=self._audit_scan_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    regime=regime,
                    btc_volatility_percentile=vol_meta.get("percentile"),
                    btc_bullish_alignment=trend_meta.get("bullish_alignment"),
                    btc_bearish_alignment=trend_meta.get("bearish_alignment"),
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"Regime snapshot recording failed ({exc})")

            return regime
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Market regime detection failed ({exc}) - defaulting to 'unknown'")
            return REGIME_UNKNOWN

    async def _process_one(self, symbol: str) -> Result | None:
        """Run the full per-symbol pipeline (stages 4a-4f).

        Returns None only for unrecoverable per-symbol errors (already
        logged). Hard-filter rejections still return a Result so the
        Research Engine can record them (Document 05 SS3: no setup is
        ever discarded).
        """
        try:
            market_data = await self.collector.fetch(symbol)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"{symbol}: data collection failed ({exc}) - skipped")
            return None

        passed, reasons = self.hard_filter.evaluate(market_data)
        if not passed:
            self.logger.debug(f"{symbol}: rejected by hard filter ({reasons})")
            if self.show_rejections:
                friendly = HardFilterEngine.explain(reasons)
                print(f"\n{symbol}\nRejected:")
                for label in friendly:
                    print(f"- {label}")

        features = [
            indicator.calculate(market_data)
            for indicator in self.indicator_registry.get_all()
        ]

        setup = self.scoring_engine.score(features, market_regime=self._market_regime)
        setup.symbol = symbol
        setup.passed = passed

        result = self.confidence_engine.evaluate(setup, batch_context=[])
        # V1.1 fix (Async/Resource Hardening revision): SQLiteRepository
        # is a synchronous interface (storage/repository.py's
        # SetupRepository is not async - changing that would be a much
        # larger interface change than this revision's scope). Calling
        # it directly here would block the event loop for every one of
        # the ~500 concurrently in-flight symbol coroutines each time
        # one finishes. asyncio.to_thread() runs the blocking call on a
        # worker thread instead, without touching the repository
        # abstraction itself.
        price_at_scan = market_data.ticker.get("last") or market_data.mark_price.get("mark_price")
        await asyncio.to_thread(self.research_engine.record, result, price_at_scan, self._config_version)

        # Tie-Break Engine input capture (see tie_break_engine.py).
        # Cheap - only a handful of floats per symbol, not the full
        # candle history - and collected here because MarketData and
        # Feature.metadata are only in scope inside this function.
        try:
            self._tie_break_inputs[symbol] = build_inputs(symbol, market_data, features)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"{symbol}: tie-break input capture failed ({exc})")

        # AI Audit Engine capture (see audit_engine.py module
        # docstring). Opt-in only, isolated so a capture failure can
        # never break the actual scan - same pattern as Performance
        # Tracker and Learning Engine.
        if self.audit_capture:
            try:
                import audit_engine
                await asyncio.to_thread(
                    audit_engine.capture_snapshot,
                    scan_id=self._audit_scan_id,
                    scan_timestamp=setup.timestamp,
                    symbol=symbol,
                    market_data=market_data,
                    features=features,
                    setup=setup,
                    passed=passed,
                    reasons=reasons,
                )
                await asyncio.to_thread(
                    audit_engine.update_confidence_for_scan,
                    scan_id=self._audit_scan_id,
                    symbol=symbol,
                    confidence=result.confidence,
                    risk_grade=result.risk_grade,
                    rank=None,  # final Top15 rank isn't known until _rank() runs on the full batch
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"{symbol}: audit capture failed ({exc})")

        return result

    async def _rank(self, results: list[Result]) -> list[Result]:
        """Rank passed setups per Document 07 SS3 priority order:
        confidence -> raw_score -> uniqueness -> relative_strength ->
        volume -> open_interest. Then applies the Tie-Break Engine
        (tie_break_engine.py) as a final, additive-only stage that
        reorders symbols still tied on (confidence, raw_score) at or
        above TIE_SCORE_THRESHOLD - it never changes raw_score or
        confidence themselves.

        V1.1 fix: uniqueness (Document 04 SS13) is relative to the
        full scan batch, but Result.uniqueness was computed per-symbol
        inside _process_one() with an empty batch_context - it was
        always the neutral default (BatchUniquenessProvider's
        _NEUTRAL_DEFAULT = 50.0), never a real rarity score. This is
        the earliest point the full batch is known, so uniqueness is
        recomputed here (in place - Result is not frozen) using every
        Setup in this scan as context, then the correction is written
        back to qde.db (which insert_setup already persisted with the
        stale value, per-symbol, before this point).
        """
        all_setups = [r.setup for r in results]
        for r in results:
            r.uniqueness = self.uniqueness_provider.calculate(r.setup, all_setups)
            try:
                await asyncio.to_thread(self.repository.update_uniqueness, r.setup.setup_id, r.uniqueness)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"{r.symbol}: uniqueness DB correction failed ({exc})")

        passed = [r for r in results if r.setup.passed]
        rejected = [r for r in results if not r.setup.passed]

        def sort_key(r: Result) -> tuple:
            fs = r.setup.feature_scores
            return (
                -r.confidence,
                -r.setup.raw_score,
                -r.uniqueness,
                -fs.get("relative_strength", 0),
                -fs.get("volume", 0),
                -fs.get("open_interest", 0),
            )

        passed.sort(key=sort_key)

        by_symbol = {r.symbol: r for r in passed}
        ordered_symbols = [r.symbol for r in passed]
        raw_scores = {r.symbol: r.setup.raw_score for r in passed}
        tie_keys = {r.symbol: (round(r.confidence, 4), round(r.setup.raw_score, 4)) for r in passed}

        try:
            new_order, tie_break_results = apply_tie_break(
                ordered_symbols, raw_scores, tie_keys, self._tie_break_inputs, self.tie_break_engine
            )
            self.tie_break_results = tie_break_results
            passed = [by_symbol[s] for s in new_order]

            # Persist for tie_break_optimizer.py (see tie_break_engine.py
            # module docstring). Always-on and cheap: only rows for
            # symbols actually involved in a real tie get written.
            if tie_break_results:
                await asyncio.to_thread(
                    record_tie_break_results,
                    scan_id=self._audit_scan_id,
                    scan_timestamp=datetime.now(timezone.utc).isoformat(),
                    tie_break_results=tie_break_results,
                    inputs_by_symbol=self._tie_break_inputs,
                    tie_keys=tie_keys,
                    weights_used=self.tie_break_engine.weights,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Tie-Break Engine failed, using primary ranking only ({exc})")

        # Deep Audit patch item #8: backfill the real Top15 rank into
        # audit.db now that the final order is known - main.py always
        # called update_confidence_for_scan() with rank=None earlier
        # (Confidence Engine runs before this batch-level ranking
        # exists), leaving top15_rank permanently NULL for every row
        # (confirmed against real production data: 7413/7413 rows).
        # Only ever narrows an existing NULL to a real number for
        # genuine Top15 members of THIS scan - never invents ranks,
        # never touches other scans.
        if self.audit_capture:
            try:
                import audit_engine

                top15 = passed[:15]
                rank_by_symbol = {r.symbol: i for i, r in enumerate(top15, start=1)}
                await asyncio.to_thread(audit_engine.backfill_top15_ranks, self._audit_scan_id, rank_by_symbol)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"Top15 rank backfill failed ({exc})")

        return passed + rejected

    @staticmethod
    def _avg(values: list[float]) -> float:
        return round(sum(values) / len(values), 2) if values else 0.0


async def main_async(args: argparse.Namespace) -> None:
    config = QDEConfig.load()
    if args.profile:
        config.general["scan_profile"] = args.profile

    # --ai-audit implies capturing audit data during this scan, since
    # the package would otherwise have nothing fresh to include.
    audit_capture = args.audit_capture or args.ai_audit

    pipeline = QDEPipeline(
        config,
        show_rejections=args.show_rejections,
        live_signals=args.live_signals,
        audit_capture=audit_capture,
    )
    try:
        results, summary = await pipeline.run()

        render_terminal(results, summary.to_dict(), pipeline.tie_break_results)
        json_path = render_json(results, summary.to_dict())
        csv_path = render_csv(results, summary.to_dict())

        QDELogger.get(__name__).info(
            f"Reports written: json={json_path} csv={csv_path}"
        )

        # QDE Performance Tracker (standalone plugin, see
        # performance_tracker.py's module docstring). Records only the
        # Top 15 already-passed signals just shown above - never
        # triggers a new scan, never touches qde.db, never affects
        # scoring/filtering/confidence/terminal output. Isolated in its
        # own try/except so a tracker failure can never break the scan.
        try:
            import performance_tracker
            await performance_tracker.record_top15(results, pipeline.exchange)
        except Exception as exc:  # noqa: BLE001
            QDELogger.get(__name__).warning(f"Performance Tracker recording failed: {exc}")

        # QDE Learning Engine (standalone plugin, see
        # learning_engine.py's module docstring). Opt-in only via
        # --learn: auto-adjusting weights.json without the operator
        # explicitly asking for it is not appropriate for a live
        # trading-signal system, especially during a livestream. Same
        # try/except isolation as the tracker above.
        if args.learn:
            try:
                import learning_engine
                report = learning_engine.analyze_and_optimize(apply=True)
                print()
                print(report)
            except Exception as exc:  # noqa: BLE001
                QDELogger.get(__name__).warning(f"Learning Engine failed: {exc}")

        # Fixed Horizon Outcome Engine (standalone plugin, see
        # outcome_engine.py module docstring - Deep Audit patch P1).
        # Runs BEFORE --ai-audit below, so a freshly-generated audit
        # package reflects any outcomes just computed here rather than
        # showing them as still-pending.
        if args.process_outcomes:
            try:
                import outcome_engine
                report = await outcome_engine.run()
                print()
                print(report)
            except Exception as exc:  # noqa: BLE001
                QDELogger.get(__name__).warning(f"Outcome Engine failed: {exc}")

        # AI Audit Engine (standalone plugin, see audit_engine.py
        # module docstring). Only packages a ZIP when --ai-audit was
        # explicitly requested - --audit-capture alone just records
        # data for a future package without generating one now.
        if args.ai_audit:
            try:
                import audit_engine
                zip_path = audit_engine.generate_package()
                print()
                print(f"AI Audit package generated: {zip_path}")
            except Exception as exc:  # noqa: BLE001
                QDELogger.get(__name__).warning(f"AI Audit Engine failed: {exc}")
    except asyncio.CancelledError:
        # V1.1 fix (Async/Resource Hardening revision): Ctrl+C during
        # `await`-suspended code raises CancelledError inside the task,
        # not KeyboardInterrupt directly - the top-level KeyboardInterrupt
        # handler in __main__ below only ever sees this if it propagates
        # all the way out, which it does after this log line and the
        # `finally` block's cleanup still run normally.
        QDELogger.get(__name__).warning("Scan cancelled by user - cleaning up.")
        raise
    except Exception as exc:  # noqa: BLE001
        # V1.1 fix (Document 10 SS8: "No top-level exception boundary").
        # Any genuinely unexpected failure (not already caught by the
        # narrower try/except blocks above) is now logged to
        # logs/qde.log via the structured logger before propagating,
        # instead of only ever reaching stderr as a raw traceback that
        # the log file would never capture.
        QDELogger.get(__name__).error(f"Scan failed with an unhandled error: {exc}")
        raise
    finally:
        # V1.1 fix (Document 10 SS2.3): both the exchange's underlying
        # aiohttp session and the SQLite connection must always be
        # closed, including on error paths, or the process exits with
        # "Unclosed client session" / "Unclosed connector" warnings and
        # leaked file descriptors.
        #
        # V1.1 fix (Async/Resource Hardening revision): each cleanup
        # call is now isolated in its own try/except - previously, if
        # exchange.close() raised, repository.close() right after it
        # would never run, silently leaking the SQLite connection.
        try:
            await pipeline.exchange.close()
        except Exception as exc:  # noqa: BLE001
            QDELogger.get(__name__).warning(f"Exchange cleanup failed: {exc}")
        try:
            pipeline.repository.close()
        except Exception as exc:  # noqa: BLE001
            QDELogger.get(__name__).warning(f"Repository cleanup failed: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QDE V1 - Quant Decision Engine")
    parser.add_argument(
        "--profile",
        choices=["fast", "balanced", "institutional", "deep_research"],
        default=None,
        help="Override the scan profile defined in config.json",
    )
    parser.add_argument(
        "--show-rejections",
        action="store_true",
        help="Print a per-symbol Hard Filter rejection breakdown "
        "(e.g. 'BTCUSDT / Rejected: - Volume (Liquidity)') as the scan "
        "runs. Off by default to keep the terminal clean.",
    )
    parser.add_argument(
        "--learn",
        action="store_true",
        help="After the scan, run the Learning Engine to analyze historical "
        "Top15 performance and auto-adjust config/weights.json if enough "
        "statistically significant evidence exists. Off by default.",
    )
    parser.add_argument(
        "--audit-capture",
        action="store_true",
        help="Record full per-indicator/per-rejection detail for every scanned "
        "symbol (not just the Top 15) into audit.db, for later AI Audit Engine "
        "packaging. Off by default. Implied by --ai-audit.",
    )
    parser.add_argument(
        "--ai-audit",
        action="store_true",
        help="Capture full audit detail during this scan (see --audit-capture) "
        "and, once the scan finishes, generate a complete AI Audit ZIP package "
        "in reports/ - a full black-box + white-box data export intended to be "
        "handed to an external LLM (Claude/ChatGPT/Gemini) for system review.",
    )
    parser.add_argument(
        "--process-outcomes",
        action="store_true",
        help="After the scan, process any pending Fixed Horizon Outcomes "
        "(outcome_engine.py) whose 30m/1h/4h/12h/24h target time has already "
        "elapsed - computed exclusively from real historical OHLCV, never a "
        "live price. Off by default; can also be run standalone at any time "
        "via 'python outcome_engine.py', independent of scanning.",
    )
    parser.add_argument(
        "--live-signals",
        action="store_true",
        help="Print a full colored block for every symbol that passes "
        "the Hard Filter, live during the scan (old V1 behavior). Off "
        "by default - this can print 100+ blocks per scan since Hard "
        "Filter passing means 'data was clean enough to score', not "
        "'this is a strong signal'. The Top 15 real signals are always "
        "shown once, in the compact report, after the scan finishes.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    # V1.1 fix (Async/Resource Hardening revision - "Graceful shutdown
    # eklensin"): Ctrl+C still triggers main_async's finally block
    # (which closes the exchange session and DB connection - Python
    # runs finally blocks on any exception, including
    # KeyboardInterrupt/CancelledError), so resource cleanup was
    # already safe before this change. What was missing was a clean
    # exit message instead of a raw traceback dump to the terminal.
    try:
        asyncio.run(main_async(parse_args()))
    except KeyboardInterrupt:
        print("\nQDE durduruldu (Ctrl+C). Kaynaklar temizlendi.")
