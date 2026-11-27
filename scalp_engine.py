#!/usr/bin/env python3
"""
QDE Agresif LONG Scalp Motoru (Final Risk Yönetimi Sürümü)
Kurallar: 3x Kaldıraç, TP1=1:2.0R, TP2=1:3.0R, 30dk Timeout, Short Pasif
"""
import sys
import sqlite3
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path("scalp_portfolio.db")
TIMEOUT_SECONDS = 1800  # 30 dakika
SCALP_COOLDOWN_SECONDS = 3600
MAX_24H_PUMP_LIMIT = 25.0
INITIAL_SUB_BALANCE = 1000.0  # Her iki kasa icin 1000$
SCALP_MARGIN_USD = 5.0
LEVERAGE = 3.0  # 3x kaldirac

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS scalp_wallets (direction TEXT PRIMARY KEY, initial_balance REAL NOT NULL DEFAULT 1000.0, current_balance REAL NOT NULL DEFAULT 1000.0, total_trades INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, updated_at TEXT NOT NULL);""")
    conn.execute("""CREATE TABLE IF NOT EXISTS scalp_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'LONG', entry_price REAL NOT NULL, stop_loss REAL NOT NULL, tp1 REAL NOT NULL, tp2 REAL NOT NULL, leverage REAL NOT NULL DEFAULT 3.0, allocated_margin REAL NOT NULL DEFAULT 5.0, status TEXT NOT NULL DEFAULT 'OPEN', current_price REAL, pnl_usd REAL DEFAULT 0.0, pnl_pct REAL DEFAULT 0.0, reason TEXT DEFAULT '', opened_at_ts REAL NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT);""")
    conn.execute("""CREATE TABLE IF NOT EXISTS scalp_cooldown (symbol TEXT PRIMARY KEY, last_trade_time REAL NOT NULL);""")
    
    now_iso = datetime.now(timezone.utc).isoformat()
    if not conn.execute("SELECT direction FROM scalp_wallets WHERE direction = 'LONG'").fetchone():
        conn.execute("INSERT INTO scalp_wallets (direction, initial_balance, current_balance, updated_at) VALUES ('LONG', ?, ?, ?)", (INITIAL_SUB_BALANCE, INITIAL_SUB_BALANCE, now_iso))
    if not conn.execute("SELECT direction FROM scalp_wallets WHERE direction = 'SHORT'").fetchone():
        conn.execute("INSERT INTO scalp_wallets (direction, initial_balance, current_balance, updated_at) VALUES ('SHORT', ?, ?, ?)", (INITIAL_SUB_BALANCE, INITIAL_SUB_BALANCE, now_iso))
    conn.commit()
    return conn

def aktif_scalp_sayisi(direction: str = "LONG") -> int:
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM scalp_positions WHERE status = 'OPEN' AND direction = ?", (direction,)).fetchone()[0]
    conn.close()
    return count

def scalp_cooldown_var_mi(symbol: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT last_trade_time FROM scalp_cooldown WHERE symbol = ?", (symbol,)).fetchone()
    conn.close()
    if row and (time.time() - row["last_trade_time"]) < SCALP_COOLDOWN_SECONDS:
        return True
    return False

def scalp_cooldown_kaydet(symbol: str):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO scalp_cooldown VALUES (?, ?)", (symbol, time.time()))
    conn.commit()
    conn.close()

def _ema(values: list[float], period: int) -> float:
    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema

def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))

def _stoch_rsi(closes: list[float], period: int = 14, k_period: int = 3, d_period: int = 3) -> tuple[float, float]:
    if len(closes) < period + k_period + d_period:
        return 50.0, 50.0
    rsis = []
    for i in range(period, len(closes)):
        rsi_val = _rsi(closes[i - period:i + 1], period)
        rsis.append(rsi_val)
    if len(rsis) < k_period:
        return 50.0, 50.0
    k_values = []
    for i in range(k_period - 1, len(rsis)):
        window = rsis[i - k_period + 1:i + 1]
        min_rsi, max_rsi = min(window), max(window)
        k = 50.0 if max_rsi == min_rsi else ((rsis[i] - min_rsi) / (max_rsi - min_rsi)) * 100
        k_values.append(k)
    if len(k_values) < d_period:
        return k_values[-1] if k_values else 50.0, 50.0
    d = sum(k_values[-d_period:]) / d_period
    return k_values[-1], d

async def scan_market_for_scalp(exchange, top_long_symbols: list[str], btc_regime: str, tickers: dict) -> dict | None:
    # Short islemleri pasif, sadece LONG
    if aktif_scalp_sayisi("LONG") >= 1:
        return None

    for sym in top_long_symbols[:15]:
        clean = sym.replace(":USDT", "").replace("/USDT", "").strip()
        if scalp_cooldown_var_mi(clean):
            continue

        t_data = tickers.get(f"{clean}/USDT:USDT") or tickers.get(f"{clean}USDT") or {}
        if float(t_data.get("percentage") or 0.0) >= MAX_24H_PUMP_LIMIT:
            continue

        try:
            # 1H Trend: Fiyat EMA20 uzerinde mi?
            raw_1h = await exchange.get_ohlcv(sym, "1h", 60)
            if not raw_1h or len(raw_1h) < 50:
                continue
            closes_1h = [c[4] for c in raw_1h]
            if closes_1h[-1] < _ema(closes_1h, 20):
                continue

            # 15m Stoch RSI: K > D (momentum yukari)
            raw_15m = await exchange.get_ohlcv(sym, "15m", 120)
            if not raw_15m or len(raw_15m) < 50:
                continue
            closes_15m = [c[4] for c in raw_15m]
            k, d = _stoch_rsi(closes_15m)
            if not (k > d):
                continue

            # 5m Kirilim, Hacim ve Mum Yapisi
            raw_5m = await exchange.get_ohlcv(sym, "5m", 30)
            if not raw_5m or len(raw_5m) < 22:
                continue
            closes_5m = [c[4] for c in raw_5m]
            opens_5m = [c[1] for c in raw_5m]
            highs_5m = [c[2] for c in raw_5m]
            lows_5m = [c[3] for c in raw_5m]
            vols_5m = [c[5] for c in raw_5m]

            current_p, current_o, current_h, current_l, current_v = closes_5m[-1], opens_5m[-1], highs_5m[-1], lows_5m[-1], vols_5m[-1]
            prior_high = max(highs_5m[-21:-1])
            if current_p <= prior_high:
                continue

            avg_vol = sum(vols_5m[-21:-1]) / 20
            if current_v < avg_vol * 1.2:
                continue

            body = abs(current_p - current_o)
            total_length = current_h - current_l
            if total_length == 0 or (body / total_length) < 0.50:
                continue

            recent_low = min(lows_5m[-4:-1])
            sl_p = recent_low * 0.998
            risk = current_p - sl_p
            if risk <= 0:
                continue

            # TP1: 1:2.0, TP2: 1:3.0 (genis hedefler)
            tp1_p = current_p + (risk * 2.0)
            tp2_p = current_p + (risk * 3.0)

            return {
                "symbol": clean, "direction": "LONG", "entry": current_p, "stop": sl_p,
                "tp1": tp1_p, "tp2": tp2_p, "stop_pct": ((current_p - sl_p) / current_p) * 100,
                "reason": "1H Trend + 15m Stoch RSI + 5m Hacimli Kirilim (Final Risk Yonetimi)"
            }
        except Exception:
            continue
    return None

def open_scalp(plan: dict):
    conn = get_conn()
    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute("""INSERT INTO scalp_positions (symbol, direction, entry_price, stop_loss, tp1, tp2, leverage, allocated_margin, status, current_price, reason, opened_at_ts, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)""", (plan["symbol"], "LONG", plan["entry"], plan["stop"], plan["tp1"], plan["tp2"], LEVERAGE, SCALP_MARGIN_USD, plan["entry"], plan["reason"], now_ts, now_iso))
    conn.commit()
    conn.close()
    scalp_cooldown_kaydet(plan["symbol"])
    print(f"\n[⚡] GUVENLI LONG ISLEME GIRDI: #{plan['symbol']} | Marjin: ${SCALP_MARGIN_USD} | Kaldirac: {LEVERAGE}x\n")

def format_scalp_report_msg(title: str, sym: str, pnl_pct: float, pnl_usd: float) -> str:
    conn = get_conn()
    w_long = conn.execute("SELECT * FROM scalp_wallets WHERE direction = 'LONG'").fetchone()
    w_short = conn.execute("SELECT * FROM scalp_wallets WHERE direction = 'SHORT'").fetchone()
    total_trades = conn.execute("SELECT COUNT(*) FROM scalp_positions WHERE status != 'OPEN'").fetchone()[0]
    total_sl = conn.execute("SELECT COUNT(*) FROM scalp_positions WHERE status IN ('CLOSED_SL', 'CLOSED_TIMEOUT')").fetchone()[0]
    total_tp1 = conn.execute("SELECT COUNT(*) FROM scalp_positions WHERE status = 'CLOSED_TP1'").fetchone()[0]
    total_tp2 = conn.execute("SELECT COUNT(*) FROM scalp_positions WHERE status = 'CLOSED_TP2'").fetchone()[0]
    conn.close()

    l_bal, s_bal = w_long["current_balance"] if w_long else 1000.0, w_short["current_balance"] if w_short else 1000.0
    l_ret, s_ret = ((l_bal - 1000.0) / 1000.0) * 100, ((s_bal - 1000.0) / 1000.0) * 100
    tot_bal = l_bal + s_bal
    tot_ret = ((tot_bal - 2000.0) / 2000.0) * 100
    pnl_sign = "+" if pnl_usd >= 0 else ""
    durum_emoji = "🟢" if pnl_usd >= 0 else "🔴"

    return f"""{title} <b>#{sym}</b> (LONG)
───────────────────────
💵 <b>İşlem Sonucu:</b> {pnl_sign}%{pnl_pct:.2f} {durum_emoji} <i>({pnl_sign}{pnl_usd:.2f} USD)</i>

📊 <b>GENEL İSTATİSTİK:</b>
Toplam İşlem: {total_trades} | Stop: {total_sl} | TP1: {total_tp1} | TP2: {total_tp2}

💼 <b>GÜNCEL SCALP KASA DURUMU:</b>
🟢 <b>Long Kasa  :</b> <code>{l_bal:.2f} USD</code> <i>({'+' if l_ret>=0 else ''}%{l_ret:.2f})</i>
🔴 <b>Short Kasa :</b> <code>{s_bal:.2f} USD</code> <i>({'+' if s_ret>=0 else ''}%{s_ret:.2f})</i>
💰 <b>Toplam Kasa:</b> <b>{tot_bal:.2f} USD</b> <i>({'+' if tot_ret>=0 else ''}%{tot_ret:.2f})</i>
───────────────────────
<i>QDE Guvenli LONG Scalp Defteri</i>"""

def check_all_scalp_exits(tickers: dict) -> list[str]:
    conn = get_conn()
    positions = conn.execute("SELECT * FROM scalp_positions WHERE status = 'OPEN'").fetchall()
    if not positions:
        conn.close()
        return []

    notifications = []
    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    for pos in positions:
        sym, margin, lev = pos["symbol"], pos["allocated_margin"] or SCALP_MARGIN_USD, pos["leverage"]
        last_p = None
        for key in [f"{sym}/USDT:USDT", f"{sym}/USDT", f"{sym}USDT", sym]:
            if key in tickers and tickers[key].get("last"):
                last_p = float(tickers[key]["last"])
                break

        entry_p, sl_p, tp1_p, tp2_p = pos["entry_price"], pos["stop_loss"], pos["tp1"], pos["tp2"]
        opened_ts = pos["opened_at_ts"] or 0
        elapsed_sec = (now_ts - opened_ts) if opened_ts > 0 else 9999

        if last_p is None and elapsed_sec >= TIMEOUT_SECONDS:
            conn.execute("UPDATE scalp_positions SET status = 'CLOSED_TIMEOUT', closed_at = ? WHERE id = ?", (now_iso, pos["id"]))
            conn.execute("UPDATE scalp_wallets SET total_trades = total_trades + 1, updated_at = ? WHERE direction = 'LONG'", (now_iso,))
            conn.commit()
            notifications.append(format_scalp_report_msg("⏱ <b>ZAMAN AŞIMI:</b>", sym, 0.0, 0.0))
            continue

        if not last_p: continue

        is_tp2, is_tp1, is_sl = (last_p >= tp2_p), (last_p >= tp1_p), (last_p <= sl_p)
        pnl_pct = ((last_p - entry_p) / entry_p) * lev * 100
        pnl_usd = margin * (pnl_pct / 100)

        if is_tp2:
            conn.execute("UPDATE scalp_positions SET status = 'CLOSED_TP2', current_price = ?, pnl_pct = ?, pnl_usd = ?, closed_at = ? WHERE id = ?", (last_p, pnl_pct, pnl_usd, now_iso, pos["id"]))
            conn.execute("UPDATE scalp_wallets SET current_balance = current_balance + ?, total_trades = total_trades + 1, wins = wins + 1, updated_at = ? WHERE direction = 'LONG'", (pnl_usd, now_iso))
            conn.commit()
            notifications.append(format_scalp_report_msg("🎯🎯 <b>SCALP TP2 VURULDU!</b>", sym, pnl_pct, pnl_usd))
        elif is_tp1:
            conn.execute("UPDATE scalp_positions SET status = 'CLOSED_TP1', current_price = ?, pnl_pct = ?, pnl_usd = ?, closed_at = ? WHERE id = ?", (last_p, pnl_pct, pnl_usd, now_iso, pos["id"]))
            conn.execute("UPDATE scalp_wallets SET current_balance = current_balance + ?, total_trades = total_trades + 1, wins = wins + 1, updated_at = ? WHERE direction = 'LONG'", (pnl_usd, now_iso))
            conn.execute("UPDATE scalp_positions SET stop_loss = entry_price WHERE symbol = ? AND status = 'OPEN'", (sym,))
            conn.commit()
            notifications.append(format_scalp_report_msg("🎯 <b>SCALP TP1 ALINDI!</b> (Stop maliyete cekildi)", sym, pnl_pct, pnl_usd))
        elif is_sl:
            conn.execute("UPDATE scalp_positions SET status = 'CLOSED_SL', current_price = ?, pnl_pct = ?, pnl_usd = ?, closed_at = ? WHERE id = ?", (last_p, pnl_pct, pnl_usd, now_iso, pos["id"]))
            conn.execute("UPDATE scalp_wallets SET current_balance = current_balance + ?, total_trades = total_trades + 1, losses = losses + 1, updated_at = ? WHERE direction = 'LONG'", (pnl_usd, now_iso))
            conn.commit()
            notifications.append(format_scalp_report_msg("🛑 <b>MIKRO STOP OLDU!</b>", sym, pnl_pct, pnl_usd))
        elif elapsed_sec >= TIMEOUT_SECONDS:
            conn.execute("UPDATE scalp_positions SET status = 'CLOSED_TIMEOUT', current_price = ?, pnl_pct = ?, pnl_usd = ?, closed_at = ? WHERE id = ?", (last_p, pnl_pct, pnl_usd, now_iso, pos["id"]))
            conn.execute("UPDATE scalp_wallets SET current_balance = current_balance + ?, total_trades = total_trades + 1, updated_at = ? WHERE direction = 'LONG'", (pnl_usd, now_iso))
            conn.commit()
            notifications.append(format_scalp_report_msg("⏱ <b>ZAMAN AŞIMI:</b>", sym, pnl_pct, pnl_usd))
        else:
            conn.execute("UPDATE scalp_positions SET current_price = ? WHERE id = ?", (last_p, pos["id"]))

    conn.commit()
    conn.close()
    return notifications
