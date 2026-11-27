"""
QDE V1 - data/exchange.py

Binance Futures (USDT-M Perpetual) client (Document 02 SS3-SS4, updated
access-layer note in Document 02's closing remarks).

Access layer policy:
    - Primary: ccxt (ccxt.async_support.binanceusdm)
    - Fallback: ccxt's implicit Binance REST API methods (e.g.
      `fapiPublicGetPremiumIndex`) for endpoints ccxt's unified API
      does not fully cover (mark price, some funding/OI history detail).
    - Data source is always Binance Futures - never Spot, never a
      third-party aggregator (Document 01 SS8).

Every request goes through RateLimiter.acquire() and is wrapped with
async_retry so a single transient failure never aborts a scan
(Document 02 SS10, SS12).

Dependency level: 1 (depends on utils.rate_limiter, utils.retry).
"""

from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt_async

from utils.rate_limiter import RateLimiter
from utils.retry import async_retry, retry_config_from_dict


class ExchangeClient:
    """Thin, rate-limited, retry-wrapped Binance USDT-M Futures client.

    All indicator/collector code depends on this class, never on ccxt
    directly (Document 02 SS3: "No indicator should directly
    communicate with Binance").
    """

    def __init__(self, rate_limit_config: dict[str, Any]) -> None:
        self._exchange = ccxt_async.binanceusdm(
            {
                "enableRateLimit": False,  # QDE's own RateLimiter governs pacing
                "options": {"defaultType": "future"},
            }
        )
        self._limiter = RateLimiter.from_config(rate_limit_config)
        self._retry_kwargs = retry_config_from_dict(rate_limit_config)

    async def _call(self, func, *args: Any, weight: float = 1.0, **kwargs: Any) -> Any:
        """Rate-limit then retry-wrap a single ccxt call."""
        await self._limiter.acquire(weight)

        @async_retry(**self._retry_kwargs, base_delay=1.0)
        async def _wrapped() -> Any:
            return await func(*args, **kwargs)

        return await _wrapped()

    async def get_active_symbols(self) -> list[str]:
        """Return active USDT-M perpetual symbols only (Document 02 SS5).

        Rejects: quarterly/delivery futures, non-USDT quotes, inverse
        contracts, inactive/delisted markets.
        """
        markets = await self._call(self._exchange.load_markets, weight=5.0, reload=True)
        symbols: list[str] = []
        for market in markets.values():
            if not market.get("active", False):
                continue
            if market.get("quote") != "USDT":
                continue
            if not market.get("swap", False):
                continue
            if not market.get("linear", False):
                continue
            info = market.get("info", {})
            if info.get("contractType") not in (None, "PERPETUAL"):
                continue
            symbols.append(market["symbol"])
        return symbols

    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int, since: int | None = None) -> list[list[float]]:
        """Returns ccxt's standard OHLCV format:
        [[timestamp, open, high, low, close, volume], ...]

        `since` (epoch milliseconds, optional): fetch candles starting
        from this point in time instead of the most recent `limit`
        candles. Added for outcome_engine.py's fixed-horizon historical
        lookups (Document "Fixed Horizon Outcome" patch) - every
        existing caller omits it and gets byte-identical behavior to
        before this parameter existed.
        """
        return await self._call(
            self._exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit, since=since
        )

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        """24h ticker stats, with spread pre-computed for Hard Filter
        Rule 7 (Document 02 SS15).
        """
        ticker = await self._call(self._exchange.fetch_ticker, symbol)
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        spread = None
        if bid and ask and bid > 0:
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid if mid else None
        return {
            "quoteVolume": ticker.get("quoteVolume"),
            "percentage": ticker.get("percentage"),
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "last": ticker.get("last"),
        }

    async def get_funding_rate(self, symbol: str) -> dict[str, Any]:
        funding = await self._call(self._exchange.fetch_funding_rate, symbol)
        return {
            "rate": funding.get("fundingRate"),
            "next_funding_time": funding.get("fundingTimestamp"),
        }

    async def get_open_interest(self, symbol: str) -> dict[str, Any]:
        oi = await self._call(self._exchange.fetch_open_interest, symbol)
        return {
            "value": oi.get("openInterestAmount") or oi.get("openInterestValue"),
            "timestamp": oi.get("timestamp"),
        }

    async def get_mark_price(self, symbol: str) -> dict[str, Any]:
        """Uses ccxt's implicit Binance REST method as fallback, since
        the unified `fetch_mark_price` is not consistently available
        across ccxt versions for binanceusdm (Document 02 closing note:
        "official fallback" policy).
        """
        market = self._exchange.market(symbol)
        raw = await self._call(
            self._exchange.fapiPublicGetPremiumIndex,
            weight=1.0,
            params={"symbol": market["id"]},
        )
        return {
            "mark_price": float(raw.get("markPrice", 0.0)) if raw else None,
            "index_price": float(raw.get("indexPrice", 0.0)) if raw else None,
        }

    # -- bulk endpoints (V1.1 - Document "Bulk API" revision) ------------
    #
    # Fetching ticker/funding/mark-price per symbol meant ~1,500
    # individual REST calls for a 500-symbol scan (Document 10 SS6.2).
    # Binance/ccxt expose bulk equivalents that return every symbol's
    # data in ONE call; DataCollector.prefetch_bulk() calls these once
    # per scan and warms the cache, so the existing per-symbol
    # _fetch_ticker/_fetch_funding/_fetch_mark_price methods find a
    # cache hit and never issue an individual request under normal
    # operation. Falling back to the individual per-symbol methods
    # above still works unchanged if a symbol is missing from a bulk
    # response for any reason.

    async def get_all_tickers(self) -> dict[str, dict[str, Any]]:
        """Bulk equivalent of get_ticker() for every symbol in one
        call. Returns {symbol: ticker_dict} using the same shape
        get_ticker() returns for a single symbol.
        """
        raw = await self._call(self._exchange.fetch_tickers, weight=40.0)
        result: dict[str, dict[str, Any]] = {}
        for symbol, ticker in raw.items():
            bid = ticker.get("bid")
            ask = ticker.get("ask")
            spread = None
            if bid and ask and bid > 0:
                mid = (bid + ask) / 2
                spread = (ask - bid) / mid if mid else None
            result[symbol] = {
                "quoteVolume": ticker.get("quoteVolume"),
                "percentage": ticker.get("percentage"),
                "bid": bid,
                "ask": ask,
                "spread": spread,
                "last": ticker.get("last"),
            }
        return result

    async def get_all_funding_rates(self) -> dict[str, dict[str, Any]]:
        """Bulk equivalent of get_funding_rate() for every symbol."""
        raw = await self._call(self._exchange.fetch_funding_rates, weight=40.0)
        return {
            symbol: {
                "rate": funding.get("fundingRate"),
                "next_funding_time": funding.get("fundingTimestamp"),
            }
            for symbol, funding in raw.items()
        }

    async def get_all_mark_prices(self) -> dict[str, dict[str, Any]]:
        """Bulk equivalent of get_mark_price(). Binance's premiumIndex
        endpoint returns an array covering every symbol when called
        without a `symbol` parameter (documented REST behavior).
        """
        raw = await self._call(self._exchange.fapiPublicGetPremiumIndex, weight=10.0)
        result: dict[str, dict[str, Any]] = {}
        for entry in raw:
            market_id = entry.get("symbol")
            if not market_id:
                continue
            try:
                unified_symbol = self._exchange.markets_by_id[market_id][0]["symbol"]
            except (KeyError, IndexError):
                continue
            result[unified_symbol] = {
                "mark_price": float(entry.get("markPrice", 0.0)),
                "index_price": float(entry.get("indexPrice", 0.0)),
            }
        return result

    async def close(self) -> None:
        """Release the underlying ccxt aiohttp session."""
        await self._exchange.close()

    async def __aenter__(self) -> "ExchangeClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
