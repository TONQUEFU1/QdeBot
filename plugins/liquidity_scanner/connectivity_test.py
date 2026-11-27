"""
QDE Liquidity Scanner Plugin - connectivity_test.py

STAGE 1 of 5: Connectivity verification.

WHY THIS FILE EXISTS SEPARATELY FROM THE PLUGIN ITSELF
    This sandbox (where the plugin code is being written) has no live
    network access to Binance - its outbound network is restricted to
    package registries only (pip/npm/github), confirmed by attempting
    a direct fetch to fapi.binance.com and having it rejected. This
    means I (the assistant) cannot personally run a live connectivity
    test against Binance's real API from here.

    What I CAN and DID verify offline (see the research report,
    approved before this stage): ccxt's actual source code for
    binanceusdm.fetch_order_book() - the real endpoint
    (GET /fapi/v1/depth), its documented weight table
    ({50: 2, 100: 5, 500: 10, 1000: 20}), and that it requires no
    authentication. That is a real verification of the API's
    *contract*, not a live *connectivity* test.

    This script is the actual connectivity test - run it yourself on
    the same Termux device/network the full scanner will run on. It
    is completely standalone: it does not import anything from the
    existing QDE codebase and does not touch qde.db, performance.db,
    or any other existing file. Safe to run any time, changes nothing.

WHAT IT MEASURES
    - Whether the endpoint responds at all (connectivity)
    - Real round-trip latency (multiple samples, min/avg/max)
    - The exact real response shape (so orderbook.py's parser can be
      validated against ground truth, not assumption)
    - Basic rate-limit behavior: fires several requests in a short
      burst and reports whether any 429/418 responses occur

USAGE (on your Termux device)
    python plugins/liquidity_scanner/connectivity_test.py
    python plugins/liquidity_scanner/connectivity_test.py BTCUSDT ETHUSDT
"""

from __future__ import annotations

import asyncio
import sys
import time

DEFAULT_TEST_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
DEPTH_LIMIT = 50  # cheapest tier per ccxt's verified weight table (cost=2)
BURST_REQUEST_COUNT = 10  # for the basic rate-limit behavior check


async def _single_fetch_test(exchange, symbol: str) -> dict:
    start = time.monotonic()
    try:
        book = await exchange.fetch_order_book(symbol, limit=DEPTH_LIMIT)
        elapsed_ms = (time.monotonic() - start) * 1000
        return {
            "symbol": symbol,
            "ok": True,
            "elapsed_ms": round(elapsed_ms, 1),
            "bid_levels": len(book.get("bids", [])),
            "ask_levels": len(book.get("asks", [])),
            "top_bid": book["bids"][0] if book.get("bids") else None,
            "top_ask": book["asks"][0] if book.get("asks") else None,
            "timestamp": book.get("timestamp"),
        }
    except Exception as exc:  # noqa: BLE001 - this IS the diagnostic, catch everything
        elapsed_ms = (time.monotonic() - start) * 1000
        return {
            "symbol": symbol,
            "ok": False,
            "elapsed_ms": round(elapsed_ms, 1),
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }


async def run_connectivity_test(symbols: list[str]) -> None:
    try:
        import ccxt.async_support as ccxt_async
    except ImportError:
        print("HATA: ccxt kurulu degil. Once calistirin: pip install ccxt")
        return

    exchange = ccxt_async.binanceusdm({"enableRateLimit": True})

    print("=" * 56)
    print("BINANCE FUTURES - LIQUIDITY SCANNER CONNECTIVITY TEST")
    print("=" * 56)
    print()

    # --- Part 1: single-request format + latency check per symbol ---
    print(f"[1/2] Tekil istek testi ({len(symbols)} sembol, depth limit={DEPTH_LIMIT})")
    print("-" * 56)
    single_results = []
    for symbol in symbols:
        result = await _single_fetch_test(exchange, symbol)
        single_results.append(result)
        if result["ok"]:
            print(
                f"  {symbol:20s} OK   {result['elapsed_ms']:7.1f}ms  "
                f"bids={result['bid_levels']:3d} asks={result['ask_levels']:3d}  "
                f"top_bid={result['top_bid']}  top_ask={result['top_ask']}"
            )
        else:
            print(
                f"  {symbol:20s} FAIL {result['elapsed_ms']:7.1f}ms  "
                f"{result['error_type']}: {result['error_message']}"
            )

    ok_results = [r for r in single_results if r["ok"]]
    print()
    if ok_results:
        latencies = [r["elapsed_ms"] for r in ok_results]
        print(
            f"  Basarili: {len(ok_results)}/{len(symbols)}  "
            f"Latency min={min(latencies):.1f}ms avg={sum(latencies)/len(latencies):.1f}ms "
            f"max={max(latencies):.1f}ms"
        )
    else:
        print("  Hicbir istek basarili olmadi - baglanti sorunu var.")

    # --- Part 2: burst test for rate-limit behavior ---
    print()
    print(f"[2/2] Hizli ardisik istek testi ({BURST_REQUEST_COUNT}x, ayni sembol, rate-limit davranisini gormek icin)")
    print("-" * 56)
    burst_symbol = symbols[0]
    burst_results = []
    for i in range(BURST_REQUEST_COUNT):
        result = await _single_fetch_test(exchange, burst_symbol)
        burst_results.append(result)
        status = "OK" if result["ok"] else f"FAIL ({result.get('error_type')})"
        print(f"  [{i+1:2d}/{BURST_REQUEST_COUNT}] {status:20s} {result['elapsed_ms']:7.1f}ms")

    burst_ok = sum(1 for r in burst_results if r["ok"])
    rate_limit_errors = [
        r for r in burst_results
        if not r["ok"] and ("429" in r.get("error_message", "") or "418" in r.get("error_message", "") or "RateLimit" in r.get("error_type", ""))
    ]
    print()
    print(f"  Basarili: {burst_ok}/{BURST_REQUEST_COUNT}")
    print(f"  Rate-limit hatasi gorulen istek sayisi: {len(rate_limit_errors)}")

    await exchange.close()

    print()
    print("=" * 56)
    print("SONUC")
    print("=" * 56)
    if ok_results and burst_ok == BURST_REQUEST_COUNT:
        print("Baglanti saglikli. Depth endpoint stabil yanit veriyor,")
        print(f"{BURST_REQUEST_COUNT} ardisik istekte rate-limit sorunu gorulmedi.")
        print("Scanner implementasyonuna gecilebilir.")
    elif ok_results:
        print("Baglanti kismen calisiyor ama burst testte sorun var -")
        print("scanner'da istekler arasi gecikme/throttling gerekebilir.")
    else:
        print("Baglanti basarisiz. Once ag/DNS/ccxt kurulumunu kontrol edin.")


if __name__ == "__main__":
    test_symbols = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_TEST_SYMBOLS
    # Accept plain "BTCUSDT" style input too, normalize to ccxt's unified format.
    normalized = []
    for s in test_symbols:
        if "/" not in s and s.upper().endswith("USDT"):
            base = s.upper()[:-4]
            normalized.append(f"{base}/USDT:USDT")
        else:
            normalized.append(s)
    asyncio.run(run_connectivity_test(normalized))
