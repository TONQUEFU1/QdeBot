"""
QDE V1 - data/collector.py

Data Collector (Document 02, entire document; Document 06 SS7 unified
market object). Orchestrates ExchangeClient + MarketCache to produce
one MarketData object per symbol. No downstream module (indicators,
engines) ever talks to the exchange or the cache directly.

Design boundary (Document 02 SS18): the collector downloads, validates,
and caches data, and assembles unified objects. It never calculates
indicators, scores setups, or produces signals.

Dependency level: 2 (depends on data.exchange, data.cache,
models.market_data).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from data.exchange import ExchangeClient
from data.cache import MarketCache
from models.market_data import MarketData, OHLCVCandle, TimeframeData


_TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


def _drop_unclosed_candle(candles: list[OHLCVCandle], timeframe: str) -> list[OHLCVCandle]:
    """Binance/ccxt's fetch_ohlcv returns the currently-forming candle
    as the last element when no `since`/end-time is given - its close
    price is still moving, so any indicator reading closes()[-1] would
    be reading a value that can repaint within the same period
    (Document 10-style look-ahead risk). Drop it if its expected close
    time hasn't passed yet.
    """
    if not candles:
        return candles
    duration_ms = _TIMEFRAME_MS.get(timeframe)
    if duration_ms is None:
        return candles  # unknown timeframe string - leave as-is rather than guess
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    last_candle_close_ms = candles[-1].timestamp + duration_ms
    if last_candle_close_ms > now_ms:
        return candles[:-1]
    return candles


class DataCollector:
    """Downloads, caches, and assembles MarketData for one or many symbols.

    Always requests more history than the strict minimum
    (Document 02 SS8: "Always download more than required"), controlled
    by `history_buffer_multiplier`.
    """

    def __init__(
        self,
        exchange: ExchangeClient,
        cache: MarketCache,
        timeframes: list[str],
        required_candles: dict[str, int],
        history_buffer_multiplier: float = 1.2,
    ) -> None:
        self.exchange = exchange
        self.cache = cache
        self.timeframes = timeframes
        self.required_candles = required_candles
        self.history_buffer_multiplier = history_buffer_multiplier

    async def fetch(self, symbol: str) -> MarketData:
        """Assemble one unified MarketData object for `symbol`
        (Document 02 SS14 output shape).
        """
        timeframe_results = await asyncio.gather(
            *[self._fetch_timeframe(symbol, tf) for tf in self.timeframes]
        )
        timeframes = dict(zip(self.timeframes, timeframe_results))

        funding, open_interest, ticker, mark_price = await asyncio.gather(
            self._fetch_funding(symbol),
            self._fetch_open_interest(symbol),
            self._fetch_ticker(symbol),
            self._fetch_mark_price(symbol),
        )

        return MarketData(
            symbol=symbol,
            timeframes=timeframes,
            funding=funding,
            open_interest=open_interest,
            ticker=ticker,
            mark_price=mark_price,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def fetch_batch(self, symbols: list[str]) -> list[MarketData]:
        """Fetch MarketData for many symbols concurrently. Per-symbol
        failures are isolated and excluded from the result rather than
        aborting the batch (Document 02 SS12).
        """
        results = await asyncio.gather(
            *[self.fetch(s) for s in symbols], return_exceptions=True
        )
        market_data_list: list[MarketData] = []
        for symbol, result in zip(symbols, results):
            if isinstance(result, Exception):
                continue  # caller (main.QDEPipeline) is responsible for logging
            market_data_list.append(result)
        return market_data_list

    async def prefetch_bulk(self, symbols: list[str]) -> None:
        """Warms the cache for ticker/funding/mark_price using bulk
        endpoints (Document "Bulk API" revision) instead of the ~1,500
        individual per-symbol REST calls a 500-symbol scan previously
        required (Document 10 SS6.2). Must be called AFTER the
        exchange's symbol universe has been loaded at least once
        (ExchangeClient.get_active_symbols() does this via
        load_markets()) - get_all_mark_prices() needs markets_by_id
        populated to map Binance's raw symbol IDs back to ccxt's
        unified symbol format.

        Safe to call even if a bulk call partially fails or omits a
        symbol: _fetch_ticker/_fetch_funding/_fetch_mark_price already
        fall back to the individual per-symbol endpoint on a cache
        miss, so missing bulk coverage degrades to the old behavior
        for just that symbol rather than failing the scan.
        """
        try:
            tickers, funding_rates, mark_prices = await asyncio.gather(
                self.exchange.get_all_tickers(),
                self.exchange.get_all_funding_rates(),
                self.exchange.get_all_mark_prices(),
            )
        except Exception:
            return  # per-symbol fallback in _fetch_* handles this scan just fine

        symbol_set = set(symbols)
        for symbol, value in tickers.items():
            if symbol in symbol_set:
                self.cache.set(symbol, "ticker", value)
        for symbol, value in funding_rates.items():
            if symbol in symbol_set:
                self.cache.set(symbol, "funding", value)
        for symbol, value in mark_prices.items():
            if symbol in symbol_set:
                self.cache.set(symbol, "mark_price", value)

    # -- per-field fetchers (cache-first) --------------------------------

    async def _fetch_timeframe(self, symbol: str, timeframe: str) -> TimeframeData:
        cached = self.cache.get(symbol, "ohlcv", timeframe)
        if cached is not None:
            return cached

        minimum = self.required_candles.get(timeframe, 100)
        # +1 to compensate for _drop_unclosed_candle potentially removing
        # the still-forming last candle (Document 10 look-ahead fix) -
        # without this, dropping one candle could leave fewer than
        # `minimum`, tripping Hard Filter Rule 3 for no real reason.
        limit = int(minimum * self.history_buffer_multiplier) + 1
        raw = await self.exchange.get_ohlcv(symbol, timeframe, limit)

        candles = [
            OHLCVCandle(
                timestamp=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in raw
        ]
        candles = _drop_unclosed_candle(candles, timeframe)
        tf_data = TimeframeData(timeframe=timeframe, candles=candles)
        self.cache.set(symbol, "ohlcv", tf_data, timeframe)
        return tf_data

    async def _fetch_funding(self, symbol: str) -> dict[str, Any]:
        cached = self.cache.get(symbol, "funding")
        if cached is not None:
            return cached
        value = await self.exchange.get_funding_rate(symbol)
        self.cache.set(symbol, "funding", value)
        return value

    async def _fetch_open_interest(self, symbol: str) -> dict[str, Any]:
        cached = self.cache.get(symbol, "open_interest")
        if cached is not None:
            return cached
        value = await self.exchange.get_open_interest(symbol)
        self.cache.set(symbol, "open_interest", value)
        return value

    async def _fetch_ticker(self, symbol: str) -> dict[str, Any]:
        cached = self.cache.get(symbol, "ticker")
        if cached is not None:
            return cached
        value = await self.exchange.get_ticker(symbol)
        self.cache.set(symbol, "ticker", value)
        return value

    async def _fetch_mark_price(self, symbol: str) -> dict[str, Any]:
        cached = self.cache.get(symbol, "mark_price")
        if cached is not None:
            return cached
        value = await self.exchange.get_mark_price(symbol)
        self.cache.set(symbol, "mark_price", value)
        return value
