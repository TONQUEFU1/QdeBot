"""
QDE V1 - tests/conftest.py

Shared pytest fixtures. Builds synthetic MarketData deterministically
so indicator/engine calculations are reproducible in tests
(Document 06 SS11: "Indicator calculations must be deterministic").
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from models.market_data import MarketData, OHLCVCandle, TimeframeData


def build_candles(n: int, start_price: float = 1.0, drift: float = 0.002, vol_spike: bool = False) -> list[OHLCVCandle]:
    candles: list[OHLCVCandle] = []
    price = start_price
    for i in range(n):
        price *= 1 + drift
        volume = 400.0 if (vol_spike and i == n - 1) else 100.0
        candles.append(
            OHLCVCandle(
                timestamp=1_700_000_000_000 + i * 60_000,
                open=price * 0.999,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=volume,
            )
        )
    return candles


@pytest.fixture
def healthy_market_data() -> MarketData:
    """A symbol with a clean bullish trend, healthy momentum, volume
    expansion, and complete funding/OI/ticker data - should pass the
    Hard Filter and score well.
    """
    timeframes = {
        "15m": TimeframeData("15m", build_candles(250, drift=0.001, vol_spike=True)),
        "1h": TimeframeData("1h", build_candles(250, drift=0.0015)),
        "4h": TimeframeData("4h", build_candles(250, drift=0.002)),
    }
    last_price = timeframes["1h"].closes()[-1]
    return MarketData(
        symbol="ETHFIUSDT",
        timeframes=timeframes,
        funding={"rate": 0.00005},
        open_interest={"value": 500000.0},
        ticker={
            "quoteVolume": 8_000_000.0,
            "percentage": 6.5,
            "btc_percentage": 2.1,
            "spread": 0.0004,
            "last": last_price,
        },
        mark_price={"mark_price": last_price},
        timestamp="2026-07-11T00:00:00Z",
    )


@pytest.fixture
def thin_market_data() -> MarketData:
    """A symbol with insufficient history and missing funding/OI -
    should fail the Hard Filter (Document 02 SS15 Rules 3-5).
    """
    return MarketData(
        symbol="THINUSDT",
        timeframes={"15m": TimeframeData("15m", build_candles(5))},
        funding={},
        open_interest={},
        ticker={"quoteVolume": 100.0, "spread": 0.02},
        mark_price={},
        timestamp="2026-07-11T00:00:00Z",
    )


@pytest.fixture
def empty_market_data() -> MarketData:
    """No timeframes, no funding, no OI at all - the extreme
    missing-data case every indicator must survive without crashing
    (Document 03 SS13).
    """
    return MarketData(
        symbol="EMPTYUSDT",
        timeframes={},
        funding={},
        open_interest={},
        ticker={},
        mark_price={},
        timestamp="2026-07-11T00:00:00Z",
    )
