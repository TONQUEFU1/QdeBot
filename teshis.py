import asyncio
from data.exchange import ExchangeClient
from scalp_engine import _ema, _rsi, _stoch_rsi

async def main():
    ex = ExchangeClient({"requests_per_second": 8.0, "max_retries": 2})
    # En likit 5 coini test edelim
    test_symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"]
    
    print("="*60)
    print("QDE SCALP FILTRE TESHIS RAPORU")
    print("="*60)

    for sym in test_symbols:
        clean = sym.split("/")[0]
        print(f"\n--- {clean} ---")
        try:
            # 1H Verisi
            raw_1h = await ex.get_ohlcv(sym, "1h", 60)
            closes_1h = [c[4] for c in raw_1h]
            ema20_1h = _ema(closes_1h, 20)
            t1h = closes_1h[-1] >= ema20_1h
            print(f"1. 1H Trend (EMA20): {'GECTI' if t1h else 'KALDI'} (Fiyat: {closes_1h[-1]:.4f}, EMA20: {ema20_1h:.4f})")

            # 15m Verisi
            raw_15m = await ex.get_ohlcv(sym, "15m", 120)
            closes_15m = [c[4] for c in raw_15m]
            highs_15m = [c[2] for c in raw_15m]
            lows_15m = [c[3] for c in raw_15m]
            
            recent_range = max(highs_15m[-20:]) - min(lows_15m[-20:])
            last_100_highs = highs_15m[-100:]
            last_100_lows = lows_15m[-100:]
            avg_100_range = sum(h - l for h, l in zip(last_100_highs, last_100_lows)) / len(last_100_highs)
            t15m_sq = recent_range <= (avg_100_range * 0.95)
            print(f"2. 15m Sikisma: {'GECTI' if t15m_sq else 'KALDI'} (Mevcut Aralik: {recent_range:.4f}, Ortalama: {avg_100_range:.4f}, Oran: {recent_range/avg_100_range:.2f})")

            k, d = _stoch_rsi(closes_15m)
            t15m_stoch = k > d
            print(f"3. 15m Stoch RSI: {'GECTI' if t15m_stoch else 'KALDI'} (K: {k:.1f}, D: {d:.1f})")

            # 5m Verisi
            raw_5m = await ex.get_ohlcv(sym, "5m", 30)
            closes_5m = [c[4] for c in raw_5m]
            opens_5m = [c[1] for c in raw_5m]
            highs_5m = [c[2] for c in raw_5m]
            lows_5m = [c[3] for c in raw_5m]
            vols_5m = [c[5] for c in raw_5m]

            prior_high = max(highs_5m[-21:-1])
            t5m_brk = closes_5m[-1] > prior_high
            print(f"4. 5m Kirilim: {'GECTI' if t5m_brk else 'KALDI'} (Fiyat: {closes_5m[-1]:.4f}, Direnc: {prior_high:.4f})")

            avg_vol = sum(vols_5m[-21:-1]) / 20
            t5m_vol = vols_5m[-1] >= avg_vol * 1.2
            print(f"5. 5m Hacim: {'GECTI' if t5m_vol else 'KALDI'} (Hacim: {vols_5m[-1]:.2f}, Ortalama: {avg_vol:.2f}, Oran: {vols_5m[-1]/avg_vol:.2f})")

            body = abs(closes_5m[-1] - opens_5m[-1])
            total_length = highs_5m[-1] - lows_5m[-1]
            body_ratio = body / total_length if total_length > 0 else 0
            t5m_body = body_ratio >= 0.50
            print(f"6. 5m Govde: {'GECTI' if t5m_body else 'KALDI'} (Oran: {body_ratio:.2f})")

        except Exception as e:
            print(f"HATA: {e}")

    await ex.close()

if __name__ == "__main__":
    asyncio.run(main())
