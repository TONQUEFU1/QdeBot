"""
QDE V1 - tests/test_hard_filter.py

Unit tests for engines/hard_filter.py (Document 02 SS15).
"""

from __future__ import annotations

from engines.hard_filter import HardFilterEngine
from models.market_data import MarketData, OHLCVCandle, TimeframeData

_FILTERS = {
    "min_quote_volume": 1_000_000,
    "max_spread": 0.01,
    "required_candles": {"15m": 200, "1h": 200, "4h": 200},
}


def test_healthy_symbol_passes(healthy_market_data):
    engine = HardFilterEngine(filters=_FILTERS)
    passed, reasons = engine.evaluate(healthy_market_data)
    assert passed is True
    assert reasons == []


def test_thin_symbol_rejected_for_multiple_reasons(thin_market_data):
    engine = HardFilterEngine(filters=_FILTERS)
    passed, reasons = engine.evaluate(thin_market_data)
    assert passed is False
    assert any("missing_or_insufficient_candles" in r for r in reasons)
    assert "missing_open_interest" in reasons
    assert "missing_funding" in reasons


def test_low_quote_volume_rejected(healthy_market_data):
    healthy_market_data.ticker["quoteVolume"] = 1.0
    engine = HardFilterEngine(filters=_FILTERS)
    passed, reasons = engine.evaluate(healthy_market_data)
    assert passed is False
    assert any("quote_volume_below_threshold" in r for r in reasons)


def test_wide_spread_rejected(healthy_market_data):
    healthy_market_data.ticker["spread"] = 0.5
    engine = HardFilterEngine(filters=_FILTERS)
    passed, reasons = engine.evaluate(healthy_market_data)
    assert passed is False
    assert any("spread_above_threshold" in r for r in reasons)


def test_corrupted_candle_data_rejected(healthy_market_data):
    bad_candle = OHLCVCandle(timestamp=1, open=1.0, high=0.5, low=0.9, close=1.0, volume=10.0)  # high < low
    healthy_market_data.timeframes["15m"] = TimeframeData(
        "15m", healthy_market_data.timeframes["15m"].candles + [bad_candle]
    )
    engine = HardFilterEngine(filters=_FILTERS)
    passed, reasons = engine.evaluate(healthy_market_data)
    assert passed is False
    assert any("corrupted_candle_data" in r for r in reasons)


def test_missing_spread_is_not_rejected(healthy_market_data):
    """V1.1 fix: unavailable spread data must not reject a symbol - this
    was the root cause of every symbol failing the Hard Filter, since
    Binance's futures ticker endpoint doesn't return bid/ask.
    """
    del healthy_market_data.ticker["spread"]
    engine = HardFilterEngine(filters=_FILTERS)
    passed, reasons = engine.evaluate(healthy_market_data)
    assert passed is True
    assert "missing_spread" not in reasons


def test_explain_maps_reasons_to_friendly_labels():
    reasons = [
        "missing_or_insufficient_candles:1d",
        "missing_open_interest",
        "missing_funding",
        "quote_volume_below_threshold:123.0",
        "spread_above_threshold:0.02",
        "corrupted_candle_data:15m",
    ]
    labels = HardFilterEngine.explain(reasons)
    assert labels == [
        "Candle History (1d)",
        "Open Interest",
        "Funding",
        "Volume (Liquidity)",
        "Spread",
        "Corrupted Data (15m)",
    ]
