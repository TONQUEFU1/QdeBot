#!/usr/bin/env python3
"""
QDE Çift Yönlü Hibrit Bot: Swing + Simetrik (Long & Short) Scalp Modülü
"""

import time
import subprocess
import sys
import json
import os
import asyncio
import urllib.request
from datetime import datetime
from pathlib import Path
import paper_engine
import scalp_engine
import entry_engine

TELEGRAM_BOT_TOKEN = "9159266248:AAHDEAoDE1_Hpz3d_P_IkXkvWYpCMeU35zY"
TELEGRAM_CHAT_ID = "-1003992142126"
PNL_TOPIC_ID = "71"
KRONOLOJI_TOPIC_ID = "325"
SCALP_TOPIC_ID = "330"
VIP_TOPIC_ID = "490"

TARAMA_ARALIGI_DAKIKA = 30
COOLDOWN_SURESI_SANIYE = 14400
COOLDOWN_DOSYASI = "sinyal_gecmisi.json"
MAX_SWING_PUMP_LIMIT = 15.0

# Drawdown korumasi - gunluk/haftalik kayip limitleri
MAX_DAILY_LOSS_PCT = 3.0
MAX_WEEKLY_LOSS_PCT = 7.0
MAX_CONSECUTIVE_LOSSES = 3

for p in [
    Path("/storage/emulated/0/BreakoutScanner/qde"),
    Path("/sdcard/BreakoutScanner/qde"),
    Path.home() / "BreakoutScanner" / "qde",
    Path.home() / "BreakoutScanner",
    Path(".")
]:
    if (p / "main.py").exists() and (p / "entry_engine.py").exists():
        os.chdir(p)
        break

def cooldown_gecmisini_yukle() -> dict:
    if Path(COOLDOWN_DOSYASI).exists():
        try:
            with open(COOLDOWN_DOSYASI, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def cooldown_kaydet(gecmis: dict):
    try:
        with open(COOLDOWN_DOSYASI, "w", encoding="utf-8") as f:
            json.dump(gecmis, f, indent=2)
    except Exception as e:
        print(f"[-] Cooldown yazılamadı: {e}")

def parite_temizle(symbol_raw: str) -> str:
    return symbol_raw.replace(":USDT", "").replace("/USDT", "").replace("#", "").strip().upper()

def cooldown_kontrol(temiz_sembol: str) -> tuple[bool, int]:
    gecmis = cooldown_gecmisini_yukle()
    simdi = time.time()
    if temiz_sembol in gecmis:
        gecen_sure = simdi - gecmis[temiz_sembol]
        if gecen_sure < COOLDOWN_SURESI_SANIYE:
            return False, max(1, int((COOLDOWN_SURESI_SANIYE - gecen_sure) / 60))
    return True, 0

def parse_signal(cikti: str) -> dict:
    d = {}
    is_rr = False
    for line in cikti.splitlines():
        line = line.strip()
        if "Risk/Getiri" in line:
            is_rr = True
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if is_rr:
                d[f"RR_{k}"] = v
            else:
                d[k] = v
    return d

def format_fiyat(fiyat: float, ref_fiyat_str: str) -> str:
    try:
        ondalik = len(ref_fiyat_str.split(".")[1]) if "." in ref_fiyat_str else 4
    except Exception:
        ondalik = 4
    ondalik = max(2, min(ondalik, 8))
    return f"{fiyat:.{ondalik}f}"

def formatla_vip(cikti: str) -> str:
    d = parse_signal(cikti)
    if not d.get("Coin"):
        return f"<code>{cikti}</code>"

    coin = parite_temizle(d.get("Coin", "BILINMIYOR"))
    yon = d.get("Yön", "LONG")
    skor = d.get("QDE Skoru", "").replace("/100", "").strip()
    guven = d.get("Güven", "").strip()
    bolge = d.get("Giriş Bölgesi", "").strip()
    giris_str = d.get("Referans Giriş", "").strip()
    stop_str = d.get("STOP LOSS", "").strip()
    kaldirac = d.get("Önerilen Kaldıraç", "2x").strip()

    try:
        ref_f = float(giris_str)
        stop_f = float(stop_str)
        risk = abs(ref_f - stop_f)

        tp1_h = ref_f + (risk * 1.8)
        tp2_h = ref_f + (risk * 3.0)

        tp1_str = f"{format_fiyat(tp1_h, giris_str)} (1:1.8)"
        tp2_str = f"{format_fiyat(tp2_h, giris_str)} (1:3.0)"
    except Exception:
        tp1_str = f"{d.get('TP1', '')} (1:1.8)"
        tp2_str = f"{d.get('TP2', '')} (1:3.0)"

    return f"""⚡️ <b>QDE QUANT ALGO — YENİ İŞLEM</b> ⚡️

🪙 <b>Parite:</b> <b>#{coin} / USDT</b>
📈 <b>Yön:</b> <b>{yon} (ALIŞ) 🟢</b>
⚙️ <b>Kaldıraç:</b> <b>{kaldirac}</b>

📥 <b>GİRİŞ:</b> <code>{giris_str}</code> <i>({bolge})</i>
⛔️ <b>STOP:</b> <code>{stop_str}</code>

🎯 <b>TP1:</b> <code>{tp1_str}</code>
🎯 <b>TP2:</b> <code>{tp2_str}</code>

🛡 <b>Onaylar:</b> Kırılım ✅ | Retest ✅ | Hacim ✅
📈 <b>Skor:</b> {skor}/100 • Güven: {guven}
───────────────────────
<i>QDE Otomatik Karar Motoru</i>"""

def telegram_mesaj_gonder(mesaj: str, topic_id: str = ""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}
    if topic_id:
        try:
            payload["message_thread_id"] = int(topic_id)
        except Exception:
            pass

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    for deneme in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                pass
            hedef = f"Konu #{topic_id}" if topic_id else "Ana Grup"
            print(f"[+] Mesaj gönderildi! ({hedef})")
            return  # Başarılıysa çık
        except Exception as e:
            print(f"[-] Telegram mesaj hatası (Deneme {deneme+1}/3): {e}")
            time.sleep(2)  # 2 saniye bekle ve tekrar dene
    print("[X] Mesaj 3 denemede gönderilemedi!")

def telegram_foto_gonder(photo_url: str, caption: str, topic_id: str = ""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": photo_url, "caption": caption, "parse_mode": "HTML"}
    if topic_id:
        try:
            payload["message_thread_id"] = int(topic_id)
        except Exception:
            pass

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=18) as response:
            pass
        print(f"[+] Sütun Grafiği İletildi! 📸")
    except Exception as e:
        print(f"[-] Grafik hatası: {e}")

def check_drawdown_limit() -> tuple[bool, str]:
    """Gunluk/haftalik kayip limitlerini kontrol eder."""
    try:
        import sqlite3
        from datetime import datetime, timezone, timedelta

        if not Path("paper_portfolio.db").exists():
            return True, ""

        conn = sqlite3.connect("paper_portfolio.db")
        conn.row_factory = sqlite3.Row

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        week_start = (now - timedelta(days=7)).isoformat()

        daily = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd), 0) as pnl FROM vault_positions "
            "WHERE closed_at >= ? AND status LIKE 'CLOSED%'", (today_start,)
        ).fetchone()
        weekly = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd), 0) as pnl FROM vault_positions "
            "WHERE closed_at >= ? AND status LIKE 'CLOSED%'", (week_start,)
        ).fetchone()

        bal = conn.execute("SELECT initial_balance FROM portfolio WHERE id = 1").fetchone()
        initial = bal["initial_balance"] if bal else 5000.0
        conn.close()

        daily_pct = (daily["pnl"] / initial) * 100 if initial > 0 else 0
        weekly_pct = (weekly["pnl"] / initial) * 100 if initial > 0 else 0

        if daily_pct <= -MAX_DAILY_LOSS_PCT:
            return False, f"Gunluk kayip limiti asildi (%{daily_pct:.2f} <= -%{MAX_DAILY_LOSS_PCT})"
        if weekly_pct <= -MAX_WEEKLY_LOSS_PCT:
            return False, f"Haftalik kayip limiti asildi (%{weekly_pct:.2f} <= -%{MAX_WEEKLY_LOSS_PCT})"
        return True, ""
    except Exception as e:
        print(f"[!] Drawdown kontrol hatasi: {e}")
        return True, ""


def send_top5_consistency_report():
    """Bugun sabah 06:00' dan itibaren en cok Top15'e giren 5 coini Telegram'a gonderir."""
    try:
        import sqlite3
        from datetime import datetime, timezone, timedelta
        
        db_path = Path("performance.db")
        if not db_path.exists():
            return
            
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        # Bugunun saat 06:00'ini hesapla (UTC)
        now = datetime.now(timezone.utc)
        today_6am = now.replace(hour=6, minute=0, second=0, microsecond=0)
        
        # Eger su anki saat 06:00'dan onceyse (gece yarisi ile 06:00 arasi), 
        # bir onceki gunun 06:00'ini baz al.
        if now < today_6am:
            today_6am = today_6am - timedelta(days=1)
            
        since = today_6am.isoformat()
        
        # En cok sinyal veren 5 coin
        rows = conn.execute("""
            SELECT symbol, COUNT(*) as count
            FROM signal_counts
            WHERE scan_timestamp >= ?
            GROUP BY symbol
            ORDER BY count DESC
            LIMIT 5
        """, (since,)).fetchall()
        conn.close()
        
        if not rows:
            print("[i] Henuz 06:00'dan itibaren sinyal kaydi yok.")
            return
            
        # Mesaji hazirla
        lines = [
            f"📊 <b>GÜNLÜK SİNYAL İSTİKRAR RAPORU</b>",
            f"<i>Başlangıç: Bugün 06:00</i>",
            "───────────────────────",
        ]
        for i, r in enumerate(rows, start=1):
            temiz_sym = r['symbol'].replace('/USDT:USDT', '').replace('USDT', '')
            lines.append(f"{i}. <b>#{temiz_sym}</b> → <b>{r['count']}</b> kez Top15'te")
        
        lines.append("───────────────────────")
        lines.append("<i>QDE Sinyal İstikrar Motoru</i>")
        
        mesaj = "\n".join(lines)
        
        # KRONOLOJI_TOPIC_ID'ye gonder
        telegram_mesaj_gonder(mesaj, topic_id=KRONOLOJI_TOPIC_ID)
        print("[+] Gunluk istikrar raporu gonderildi.")
    except Exception as e:
        print(f"[-] Istikrar raporu hatasi: {e}")


def get_latest_btc_regime() -> str:
    """QDE tarafindan en son tespit edilen piyasa rejimini (regime) okur."""
    try:
        import sqlite3
        conn = sqlite3.connect("qde.db")
        r = conn.execute("SELECT regime FROM regime_snapshots ORDER BY timestamp DESC LIMIT 1").fetchone()
        conn.close()
        if r and r[0]:
            return r[0]
        return "unknown"
    except Exception as e:
        print(f"[!] Rejim okuma hatasi: {e}")
        return "unknown"

async def run_dual_scalp_cycle(top_symbols: list[str], btc_regime: str):
    from data.exchange import ExchangeClient
    ex = ExchangeClient(rate_limit_config={"requests_per_second": 8.0, "max_retries": 3})
    try:
        print(f"  [SCALP] Fiyatlar çekildi. Açık pozisyonlar güncelleniyor...")
        tickers = await ex.get_all_tickers()

        # 1. Swing Kasalarını Güncelle
        paper_engine.update_all_with_prices(tickers)

        # 2. Açık Scalp Çıkışlarını Denetle
        notifications = scalp_engine.check_all_scalp_exits(tickers)
        for notif in notifications:
            telegram_mesaj_gonder(notif, topic_id=SCALP_TOPIC_ID)

        # 3. Canlı Arama Durumu
        l_count = scalp_engine.aktif_scalp_sayisi("LONG")
        s_count = scalp_engine.aktif_scalp_sayisi("SHORT")
        print(f"  [Scalp 5m] Aktif Long: {l_count}/1 | Aktif Short: {s_count}/1 | Rejim: {btc_regime}")

        # 4. Long (Top 15) ve Short (En Zayıf 15) Adaylarında Kırılım Ara
        firsat = await scalp_engine.scan_market_for_scalp(ex, top_symbols, btc_regime, tickers)
        if firsat:
            scalp_engine.open_scalp(firsat)
            yon = firsat["direction"]
            yon_ikon = "LONG 🟢" if yon == "LONG" else "SHORT 🔴"
            kart = f"""⚡️ <b>QDE MİKRO SCALP — YENİ İŞLEM</b> ⚡️
───────────────────────
🪙 <b>Parite:</b> <b>#{firsat['symbol']} / USDT</b> <i>(5m Kırılım)</i>
📈 <b>Yön:</b> <b>{yon_ikon}</b> | <b>Kaldıraç:</b> <b>5x</b>

📥 <b>GİRİŞ:</b> <code>{firsat['entry']:.6g}</code>
🛑 <b>MİKRO STOP:</b> <code>{firsat['stop']:.6g}</code> <i>(-%{firsat['stop_pct']:.2f})</i>

🎯 <b>TP1:</b> <code>{firsat['tp1']:.6g}</code> (1:1.2 R)
🎯 <b>TP2:</b> <code>{firsat['tp2']:.6g}</code> (1:2.0 R)

💡 <b>Sinyal Nedeni:</b> {firsat['reason']}
⏱ <b>Zaman Limiti:</b> 35 Dakika <i>(Hacim sönerse kapatılır)</i>
🛡 <b>Slot:</b> 1 Long / 1 Short Dengeli Mod
───────────────────────
<i>QDE Çift Yönlü Scalp Motoru</i>"""
            telegram_mesaj_gonder(kart, topic_id=SCALP_TOPIC_ID)
    finally:
        await ex.close()

def main(once=False):
    print("=" * 54)
    print("QDE SİMETRİK BOT: TOP 15 LONG & TOP 15 SHORT AKTİF")
    print(f"Grup: {TELEGRAM_CHAT_ID} | Scalp Konusu: #{SCALP_TOPIC_ID}")
    print("=" * 54)

    last_day = datetime.now().day

    while True:
        if once:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Tek tur modu baslatildi (--once).")
        simdi = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        print(f"\n[{simdi}] 1. Aşama: 500+ Coin Taranıyor...")

        # Makro risk kontrolü kaldırıldı (sinyal anında kontrol edilecek)

        current_day = datetime.now().day
        if current_day != last_day:
            paper_engine.record_daily_close()
            c_url, c_cap = paper_engine.generate_bar_chart_url_and_caption()
            telegram_foto_gonder(c_url, c_cap, topic_id=KRONOLOJI_TOPIC_ID)
            last_day = current_day

        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama başlatıldı. Lütfen bekleyin...")
            subprocess.run([sys.executable, "-u", "main.py"], timeout=600, check=True)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [+] Tarama tamamlandı. Raporlar yazıldı.")
        except Exception as e:
            print(f"[-] Tarama hatası: {e}")
            time.sleep(30)
            continue

        btc_regime = get_latest_btc_regime()
        print(f"[*] Piyasa Rejimi: {btc_regime}")

        print(f"[{simdi}] 2. Aşama: Swing & Kasa Yönetimi...")

        izin_var, dd_sebep = check_drawdown_limit()
        if not izin_var:
            print(f"[DRAWDOWN] {dd_sebep} - Swing sinyalleri atlaniyor")
        top_symbols = []
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Swing Sinyali Kontrol Ediliyor (Entry Engine)...")
            res = subprocess.run([sys.executable, "-u", "entry_engine.py"], capture_output=True, text=True, timeout=180)
            cikti = res.stdout.strip()
            print(f"\n--- ENTRY ENGINE CIKTISI ---\n{cikti}\n----------------------------", flush=True)

            if "QDE LONG SİNYALİ" in cikti:
                parsed = parse_signal(cikti)
                coin_temiz = parite_temizle(parsed.get("Coin", ""))
                izin_var, kalan_dk = cooldown_kontrol(coin_temiz)

                from data.exchange import ExchangeClient
                async def _check_pump(sym):
                    ex_p = ExchangeClient(rate_limit_config={"requests_per_second": 8.0, "max_retries": 2})
                    try:
                        tk = await ex_p.get_ticker(sym)
                        return abs(float(tk.get("percentage") or 0.0))
                    finally:
                        await ex_p.close()

                change_24h = asyncio.run(_check_pump(parsed.get("Coin", "")))
                if change_24h >= MAX_SWING_PUMP_LIMIT:
                    print(f"[ENGEL] #{coin_temiz} 24s %{change_24h:.1f} yükselmiş. Tepeden alım engellendi!")
                elif izin_var:
                    vip_kart = formatla_vip(cikti)
                    telegram_mesaj_gonder(vip_kart, topic_id=VIP_TOPIC_ID)
                    paper_engine.process_new_signal(parsed)
                    gecmis = cooldown_gecmisini_yukle()
                    gecmis[coin_temiz] = time.time()
                    cooldown_kaydet(gecmis)

            latest_cands = entry_engine.fetch_latest_top15()
            top_symbols = [c.symbol for c in latest_cands]
        except Exception as e:
            print(f"[-] Swing hatası: {e}")

        # 3. Aşama: 5 Dakikalık Mikro Scalp Takip Döngüsü (Long Top 15 + Short Top 15)
        print(f"[{simdi}] 3. Aşama: 5m Simetrik Scalp Takibi Başlatılıyor... (6 tur, her tur 5 dk)")
        sub_dongu_sayisi = 6
        for adim in range(sub_dongu_sayisi):
            try:
                print(f"  [SCALP] Döngü {adim+1}/{sub_dongu_sayisi} başlatıldı...", flush=True)
                asyncio.run(run_dual_scalp_cycle(top_symbols, btc_regime))
            except Exception as e_sc:
                print(f"[-] Scalp döngü hatası: {e_sc}")

            if adim < sub_dongu_sayisi - 1:
                kalan_dk = 5
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Scalp turu {adim+1}/{sub_dongu_sayisi} bitti. {kalan_dk} dk bekleniyor...")
                for saniye in range(300, 0, -60):
                    time.sleep(60)
                    if saniye > 60:
                        print(f"   ... kalan {saniye//60 - 1} dk", end="\r")

        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] PnL Raporu oluşturuluyor...")
            pnl_rapor = paper_engine.generate_two_layer_report()
            telegram_mesaj_gonder(pnl_rapor, topic_id=PNL_TOPIC_ID)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Döngü tamamlandı. Yeniden taranıyor...")
            
            # Gunluk sinyal istikrar raporu
            send_top5_consistency_report()
            
            # Tek tur modu ise donguden cik
            if once:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Tek tur tamamlandi. Cikiliyor.")
                break
        except Exception as e:
            print(f"[-] PnL raporu hatası: {e}")

if __name__ == "__main__":
    import sys
    once_mode = "--once" in sys.argv
    main(once=once_mode)
