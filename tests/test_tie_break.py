"""
QDE V1.1 - tests/test_tie_break.py

Unit tests for tie_break_engine.py (Document 12).
"""

from __future__ import annotations

from tie_break_engine import TieBreakEngine, TieBreakInputs, TieBreakResult, apply_tie_break


def _strong_inputs(symbol: str) -> TieBreakInputs:
    return TieBreakInputs(
        symbol=symbol,
        trend_bullish_alignment=True,
        ema20=105, ema50=102, ema200=95,
        rsi=55,
        volume_ratio=1.8,
        volatility_percentile=25,
        pct_change_24h=8.0,
        avg_wick_ratio=1.2,
        quote_volume=5_000_000,
        spread=0.0004,
    )


def _weak_inputs(symbol: str) -> TieBreakInputs:
    return TieBreakInputs(
        symbol=symbol,
        trend_bullish_alignment=False,
        ema20=98, ema50=99, ema200=100,
        rsi=40,
        volume_ratio=0.8,
        volatility_percentile=85,
        pct_change_24h=1.0,
        avg_wick_ratio=1.5,
        quote_volume=1_000_000,
        spread=0.002,
    )


def _risky_pump_inputs(symbol: str) -> TieBreakInputs:
    return TieBreakInputs(
        symbol=symbol,
        trend_bullish_alignment=True,
        ema20=110, ema50=100, ema200=90,
        rsi=78,
        volume_ratio=5.5,
        volatility_percentile=70,
        pct_change_24h=35.0,
        avg_wick_ratio=3.2,
        quote_volume=2_000_000,
        spread=0.001,
    )


def test_strong_signal_scores_higher_than_weak_signal():
    engine = TieBreakEngine()
    strong = engine.calculate(_strong_inputs("STRONG"))
    weak = engine.calculate(_weak_inputs("WEAK"))
    assert strong.total > weak.total


def test_risk_penalty_reduces_total_for_already_pumped_symbol():
    engine = TieBreakEngine()
    result = engine.calculate(_risky_pump_inputs("PUMPED"))
    assert result.risk_penalty > 0


def test_result_is_explainable():
    engine = TieBreakEngine()
    result = engine.calculate(_strong_inputs("X"))
    explanation = result.explain()
    assert "Trend" in explanation
    assert "Toplam" in explanation
    assert set(result.breakdown.keys()) == {"trend", "momentum", "volume", "breakout", "risk"}


def test_missing_data_returns_neutral_zero_scores():
    engine = TieBreakEngine()
    empty = TieBreakInputs(symbol="EMPTY")
    result = engine.calculate(empty)
    assert result.total == 0.0


def test_apply_tie_break_reorders_only_genuine_ties_above_threshold():
    symbols = ["A", "B", "C", "D"]
    raw_scores = {"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0}
    tie_keys = {s: (100.0, 100.0) for s in symbols}
    inputs = {
        "A": _weak_inputs("A"),
        "B": _strong_inputs("B"),
        "C": _risky_pump_inputs("C"),
        "D": _strong_inputs("D"),
    }

    new_order, results = apply_tie_break(symbols, raw_scores, tie_keys, inputs)

    assert new_order[0] in ("B", "D")  # the two strong ones should lead
    assert new_order[-1] == "A"  # weak trend/volume should trail
    assert len(results) == 4


def test_apply_tie_break_ignores_non_tied_symbols():
    symbols = ["A", "B", "C"]
    raw_scores = {"A": 100.0, "B": 98.0, "C": 96.0}
    tie_keys = {"A": (100.0, 100.0), "B": (99.0, 98.0), "C": (98.0, 96.0)}
    inputs = {s: TieBreakInputs(symbol=s) for s in symbols}

    new_order, results = apply_tie_break(symbols, raw_scores, tie_keys, inputs)

    assert new_order == symbols
    assert results == {}


def test_apply_tie_break_ignores_ties_below_threshold():
    symbols = ["X", "Y"]
    raw_scores = {"X": 80.0, "Y": 80.0}
    tie_keys = {"X": (80.0, 80.0), "Y": (80.0, 80.0)}
    inputs = {s: TieBreakInputs(symbol=s) for s in symbols}

    new_order, results = apply_tie_break(symbols, raw_scores, tie_keys, inputs)

    assert new_order == symbols
    assert results == {}


def test_volume_score_prefers_sustained_expansion_over_single_spike():
    engine = TieBreakEngine()
    sustained = TieBreakInputs(symbol="A", volume_ratio=1.8)
    spike = TieBreakInputs(symbol="B", volume_ratio=8.0)
    assert engine._score_volume(sustained) > engine._score_volume(spike)
