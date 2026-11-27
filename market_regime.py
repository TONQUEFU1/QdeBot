"""
QDE V1.1 - market_regime.py

QDE Market Regime Classifier (standalone module, part of the V1 final
revision - see conversation Document "Market Regime" requirement).

WHAT THIS MODULE DOES
    Classifies the current scan's market regime as one of:
        bull | bear | sideways | high_volatility
    using BTCUSDT's own already-computed indicator metadata - no new
    indicators, no new API calls, no external data source. This
    directly replaces the market_regime="unknown" gap flagged in
    Document 10 SS2.2 and left unresolved through several prior
    revisions.

WHY BTC, AND WHY THESE TWO SIGNALS
    BTC is the standard reference asset for overall crypto market
    direction - using it (rather than averaging across all 500+ scanned
    symbols) avoids an extra aggregation step and matches how the
    project's own design documents (Document 04 SS12, Document 10's
    Missing Data Report) already frame "market regime" as a BTC-trend
    concept.
    - Trend direction: indicators/trend.py already computes
      `bullish_alignment` / `bearish_alignment` from EMA20/50/200
      ordering - reused here verbatim, not recomputed.
    - Volatility level: indicators/volatility.py already computes a
      Bollinger Band width `percentile` (relative to the symbol's own
      recent history) - reused here verbatim.
    High volatility is checked FIRST and takes precedence over
    trend direction, since a violently volatile market (in either
    direction) changes signal reliability more than which direction
    it's moving in (Document 04 SS12: "High Volatility: Expected Move
    increases, Expected Risk also increases" applies regardless of
    trend).

HONEST LIMITATION
    This is a simple, two-signal heuristic, not a validated regime
    model. It has not been backtested against realized outcomes (no
    backtest infrastructure exists yet - Document 10's Missing Data
    Report). It replaces a worse baseline (a constant "unknown" that
    fed into nothing) with a real, if simple, signal - not a claim of
    statistical rigor.

USAGE
    Computed once per scan (main.QDEPipeline.run(), from BTCUSDT's own
    MarketData/Features, fetched once before the main per-symbol loop)
    and passed as a single string into ScoringEngine.score() and
    (via Setup.market_regime) ConfidenceEngine.evaluate() for the
    whole batch - the regime does not change mid-scan.
"""

from __future__ import annotations

from models.feature import Feature

REGIME_BULL = "bull"
REGIME_BEAR = "bear"
REGIME_SIDEWAYS = "sideways"
REGIME_HIGH_VOLATILITY = "high_volatility"
REGIME_UNKNOWN = "unknown"

VALID_REGIMES = (REGIME_BULL, REGIME_BEAR, REGIME_SIDEWAYS, REGIME_HIGH_VOLATILITY, REGIME_UNKNOWN)

# Matches indicators/volatility.py's own _LOOSE_PERCENTILE threshold
# (already used there to mean "expanded / no compression") - reused
# here for consistency rather than inventing a second constant.
HIGH_VOLATILITY_PERCENTILE_THRESHOLD = 80.0


def classify_regime(features: list[Feature]) -> str:
    """Classifies market regime from BTC's own computed Feature list
    (the same `features` list main.QDEPipeline._process_one() already
    builds for every symbol - BTC is processed once, specifically for
    this purpose, before the main scan loop).

    Returns REGIME_UNKNOWN only if BTC's trend/volatility features
    were themselves unavailable (e.g. insufficient candle history) -
    an honest "don't know" rather than guessing.
    """
    by_name = {f.name: f for f in features}

    volatility = by_name.get("volatility")
    if volatility is not None:
        percentile = volatility.metadata.get("percentile")
        if percentile is not None and percentile >= HIGH_VOLATILITY_PERCENTILE_THRESHOLD:
            return REGIME_HIGH_VOLATILITY

    trend = by_name.get("trend")
    if trend is not None:
        if trend.metadata.get("available", True) is False:
            return REGIME_UNKNOWN
        if trend.metadata.get("bullish_alignment"):
            return REGIME_BULL
        if trend.metadata.get("bearish_alignment"):
            return REGIME_BEAR
        return REGIME_SIDEWAYS

    return REGIME_UNKNOWN
