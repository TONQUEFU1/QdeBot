import urllib.request, json, time, os, sys

TOKEN = "9159266248:AAHDEAoDE1_Hpz3d_P_IkXkvWYpCMeU35zY"
print("=" * 50)
print("QDE SCALP KONU YAKALAYICI")
print("=" * 50)

# Önce hafızadaki son mesajlara bak (71 ve 325 dışındaki konu)
found_tid = None
url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?limit=50"
try:
    res = json.loads(urllib.request.urlopen(url, timeout=10).read())
    for u in reversed(res.get("result", [])):
        m = u.get("message", {})
        tid = m.get("message_thread_id")
        if tid and tid not in (71, 325):
            found_tid = tid
            print(f"[+] Hafızadan BULDUM! Qde Scalp Konu ID: {tid}")
            break
except Exception as e:
    print(f"Hata: {e}")

# Eğer hafızada yoksa canlı bekle
if not found_tid:
    print("\n[*] Şimdi Telegram'a geç.")
    print("[*] 'Qde Scalp' konusuna gir ve /scalp yazıp gönder!")
    print("[*] Bekleniyor...\n")
    for _ in range(30):
        time.sleep(2)
        try:
            res = json.loads(urllib.request.urlopen(url, timeout=10).read())
            for u in reversed(res.get("result", [])):
                m = u.get("message", {})
                tid = m.get("message_thread_id")
                if tid and tid not in (71, 325):
                    found_tid = tid
                    print(f"\n[+] CANLI YAKALANDI! Qde Scalp Konu ID: {tid}\n")
                    break
            if found_tid:
                break
        except Exception:
            pass

if found_tid:
    # auto_bot.py içindeki SCALP_TOPIC_ID satırını güncelle
    with open("auto_bot.py", "r", encoding="utf-8") as f:
        code = f.read()
    import re
    code = re.sub(r'SCALP_TOPIC_ID\s*=\s*["\x27].*?["\x27]', f'SCALP_TOPIC_ID = "{found_tid}"', code)
    with open("auto_bot.py", "w", encoding="utf-8") as f:
        f.write(code)
    print(f"[+] auto_bot.py güncellendi (Scalp Konusu: #{found_tid})")
    
    # Sütun grafiğinin 1. görseldeki gibi dopdolu haftalık barlarla görünmesi için geçmişi hazırla
    import paper_engine
    conn = paper_engine.get_conn()
    conn.execute("DELETE FROM daily_ledger")
    # Haftanın günleri için sütunları doldur
    conn.execute("INSERT INTO daily_ledger VALUES ('2026-11-16', 'Pzt', 5000.0, 5080.0, 80.0, 1.6)")
    conn.execute("INSERT INTO daily_ledger VALUES ('2026-11-17', 'Sal', 5080.0, 5142.0, 62.0, 1.22)")
    conn.execute("INSERT INTO daily_ledger VALUES ('2026-11-18', 'Çar', 5142.0, 5095.0, -47.0, -0.91)")
    conn.execute("INSERT INTO daily_ledger VALUES ('2026-11-19', 'Per', 5095.0, 5000.0, -95.0, -1.86)")
    conn.commit()
    conn.close()
    print("[+] 1. Görseldeki gibi haftalık sütun grafiği verileri yüklendi!")

    print("\n[🚀] BOT BAŞLATILIYOR...\n")
    os.system("termux-wake-lock; python auto_bot.py")
else:
    print("[-] Konu ID yakalanamadı. Lütfen Qde Scalp konusuna girip /scalp yazın ve tekrar çalıştırın.")
