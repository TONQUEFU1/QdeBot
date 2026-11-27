"""
QDE V1.1 - tests/test_look_ahead.py

Regression tests for the look-ahead / repainting fix
(data/collector.py's _drop_unclosed_candle). Added in the V1 final
revision specifically because the independent audit found this
behavior had NO permanent test coverage despite being a critical fix -
only verified via ad-hoc scripts that left no lasting protection
against regression.
"""

from __future__ import annotations

from datetime import datetime, timezone

from data.collector import _drop_unclosed_candle, _TIMEFRAME_MS, DataCollector
from models.market_data import OHLCVCandle


def _make_candles(n: int, duration_ms: int, now_ms: int, last_is_unclosed: bool) -> list[OHLCVCandle]:
    candles = []
    for i in range(n):
        ts = now_ms - (n - i) * duration_ms
        candles.append(OHLCVCandle(timestamp=ts, open=1.0, high=1.1, low=0.9, close=1.0, volume=100.0))
    if last_is_unclosed:
        # Open time only half a period ago -> close time is still in the future.
        candles[-1] = OHLCVCandle(
            timestamp=now_ms - duration_ms // 2, open=1.0, high=1.1, low=0.9, close=1.0, volume=100.0
        )
    return candles


def test_unclosed_last_candle_is_dropped():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    duration = _TIMEFRAME_MS["15m"]
    candles = _make_candles(20, duration, now_ms, last_is_unclosed=True)

    result = _drop_unclosed_candle(candles, "15m")

    assert len(result) == 19
    assert result[-1].timestamp == candles[-2].timestamp


def test_closed_last_candle_is_kept():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    duration = _TIMEFRAME_MS["15m"]
    candles = _make_candles(20, duration, now_ms, last_is_unclosed=False)

    result = _drop_unclosed_candle(candles, "15m")

    assert len(result) == 20
    assert result[-1].timestamp == candles[-1].timestamp


def test_empty_candle_list_returns_empty():
    assert _drop_unclosed_candle([], "15m") == []


def test_unknown_timeframe_string_is_left_unchanged():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles = _make_candles(10, 60_000, now_ms, last_is_unclosed=True)
    result = _drop_unclosed_candle(candles, "3m")  # not in _TIMEFRAME_MS
    assert len(result) == 10  # unchanged - no guessing at an unmapped duration


def test_all_configured_timeframes_covered():
    """Document requirement: the fix must apply to every timeframe QDE
    actually scans with (config/config.json's ["15m","1h","4h","1d"]),
    not just 15m.
    """
    import json
    from pathlib import Path

    config = json.loads((Path(__file__).resolve().parent.parent / "config" / "config.json").read_text())
    for tf in config["timeframes"]:
        assert tf in _TIMEFRAME_MS, f"{tf} is configured but missing from _TIMEFRAME_MS"


def test_minimum_candle_count_preserved_after_drop():
    """For every configured timeframe, fetching (required_minimum *
    buffer_multiplier) + 1 candles and dropping one unclosed candle
    must still leave at least required_minimum closed candles -
    otherwise the look-ahead fix would itself trip Hard Filter Rule 3
    for symbols that would have passed before this fix.
    """
    import json
    from pathlib import Path

    config = json.loads((Path(__file__).resolve().parent.parent / "config" / "config.json").read_text())
    required_candles = config["required_candles"]
    multiplier = 1.2  # DataCollector's default history_buffer_multiplier
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    for tf, minimum in required_candles.items():
        limit = int(minimum * multiplier) + 1
        duration = _TIMEFRAME_MS[tf]
        candles = _make_candles(limit, duration, now_ms, last_is_unclosed=True)
        result = _drop_unclosed_candle(candles, tf)
        assert len(result) >= minimum, (
            f"{tf}: only {len(result)} closed candles after drop, need >= {minimum}"
        )


async def test_data_collector_applies_the_fix_end_to_end():
    """Integration check: DataCollector._fetch_timeframe actually calls
    the drop logic and requests one extra candle to compensate -
    verified against a mocked exchange returning a deliberately
    unclosed last candle.
    """
    from unittest.mock import AsyncMock
    from data.cache import MarketCache

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    duration = _TIMEFRAME_MS["15m"]
    minimum = 50
    limit_requested = int(minimum * 1.2) + 1

    raw_rows = []
    for i in range(limit_requested):
        ts = now_ms - (limit_requested - i) * duration
        raw_rows.append([ts, 1.0, 1.1, 0.9, 1.0, 100.0])
    raw_rows[-1] = [now_ms - duration // 2, 1.0, 1.1, 0.9, 1.0, 100.0]  # unclosed

    fake_exchange = AsyncMock()
    fake_exchange.get_ohlcv = AsyncMock(return_value=raw_rows)

    collector = DataCollector(
        exchange=fake_exchange,
        cache=MarketCache(ttl_config={}, default_ttl=120),
        timeframes=["15m"],
        required_candles={"15m": minimum},
    )

    tf_data = await collector._fetch_timeframe("TESTUSDT", "15m")

    fake_exchange.get_ohlcv.assert_awaited_once()
    call_args = fake_exchange.get_ohlcv.await_args
    assert call_args.args[2] == limit_requested or call_args[0][2] == limit_requested

    assert len(tf_data.candles) == limit_requested - 1  # the unclosed one was dropped
    assert len(tf_data.candles) >= minimum
