"""
QDE News & Sentiment Filter (Smart Context-Aware Rules)
- Haberleri çeker.
- Makro ve mikro riskleri "bağlam" (context) kelimeleriyle analiz eder.
"""
import urllib.request
import json
import time

API_BASE = "https://cryptocurrency.cv"
CACHE_TTL = 300
_cache = {"timestamp": 0, "articles": []}

# --- Bağlam Sözlükleri ---
# Savaş/Jeo-politik
WAR_WORDS = ["war", "missile", "invasion", "attack", "conflict"]
WAR_NEGATIVE = ["nuclear", "escalates", "crypto", "bitcoin", "market", "crash"]
WAR_POSITIVE = ["peace", "ceasefire", "truce", "agreement", "ends", "de-escalates", "talks"]

# Hack/Güvenlik
HACK_WORDS = ["hack", "exploit", "breach", "vulnerabilit", "drain", "phish", "steal", "attack"]
HACK_NEGATIVE = ["lost", "stolen", "drained", "compromised", "suffered", "exploit", "attack", "funnel", "million", "million"]
HACK_POSITIVE = ["prevented", "secured", "patched", "recovered", "audit", "safe"]

# Diğer Makro Riskler (Direkt olumsuz)
MACRO_RISK_WORDS = ["sec lawsuit", "regulation ban", "bankruptcy", "insolvency", "crash", "plunge"]

# Coin Bazlı Direkt Riskler
COIN_RISK_WORDS = ["delist", "rug pull", "scam", "fraud", "lawsuit", "suspend"]

def _get(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={'User-Agent': 'QDE-Bot/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fetch_news(force=False) -> list:
    global _cache
    now = time.time()
    if not force and (now - _cache["timestamp"]) < CACHE_TTL and _cache["articles"]:
        return _cache["articles"]
    try:
        data = _get(f"{API_BASE}/feed.json")
        items = data.get("items", data) if isinstance(data, dict) else data
        _cache["articles"] = items
        _cache["timestamp"] = now
        return items
    except Exception as e:
        print(f"[!] Haber cekme hatasi: {e}")
        return _cache["articles"]

def _analyze_context(title: str, trigger_words: list, negative_words: list, positive_words: list) -> int:
    """Kelimenin gectigi baglami analiz eder. Negatifse -1, pozitifse +1, notrse 0 doner."""
    # "Exploit" kelimesi aranirken "exploited", "exploiting" gibi ekleri de yakalamak icin
    # basit bir substring kontrolu kullaniyoruz.
    if not any(w in title for w in trigger_words):
        return 0
    
    has_neg = any(n in title for n in negative_words)
    has_pos = any(p in title for p in positive_words)
    
    if has_neg and not has_pos:
        return -1
    elif has_pos and not has_neg:
        return 1
    else:
        return 0 # Baglam belirsiz veya ikisi de var

def analyze_macro_risk() -> tuple:
    articles = fetch_news()
    if not articles:
        return False, 0, []
    
    risk_score = 0
    hits = []
    
    for a in articles:
        title = (a.get("title", "") or "").lower()
        
        # 1. Direkt Makro Risk
        for w in MACRO_RISK_WORDS:
            if w in title:
                risk_score += 2
                hits.append(w)
        
        # 2. Savas Baglam Analizi
        war_context = _analyze_context(title, WAR_WORDS, WAR_NEGATIVE, WAR_POSITIVE)
        if war_context == -1:
            risk_score += 1
            hits.append("war_negative")
            
        # 3. Hack Baglam Analizi
        hack_context = _analyze_context(title, HACK_WORDS, HACK_NEGATIVE, HACK_POSITIVE)
        if hack_context == -1:
            risk_score += 1
            hits.append("hack_negative")
            
    # Sadece skor 5 ve uzerindeyse piyasayi durdur (cok ciddi durumlar icin)
    is_risky = risk_score >= 7
    return is_risky, min(10, risk_score), list(set(hits))

def analyze_coin_news(symbol: str) -> dict:
    clean = symbol.split("/")[0].replace(":USDT", "").upper()
    articles = fetch_news()
    if not articles:
        return {"risk": "UNKNOWN", "sentiment": "NEUTRAL", "score": 0, "news": []}

    matched = []
    rule_score = 0

    for a in articles:
        title = (a.get("title", "") or "").lower()
        if clean.lower() not in title:
            continue
        matched.append(a.get("title", ""))

        # 1. Direkt Coin Riskleri (delist, rug pull, scam)
        if any(w in title for w in COIN_RISK_WORDS):
            rule_score -= 5
            
        # 2. Hack Baglam Analizi (Coin'e ozel)
        hack_context = _analyze_context(title, HACK_WORDS, HACK_NEGATIVE, HACK_POSITIVE)
        if hack_context == -1:
            rule_score -= 4
        elif hack_context == 1:
            rule_score += 2

    if rule_score <= -4:
        risk = "HIGH"
    elif rule_score < 0:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    if rule_score >= 2:
        sentiment = "POSITIVE"
    elif rule_score < -2:
        sentiment = "NEGATIVE"
    else:
        sentiment = "NEUTRAL"

    return {
        "risk": risk,
        "sentiment": sentiment,
        "score": rule_score,
        "news": matched[:3],
    }

if __name__ == "__main__":
    print("=== SMART CONTEXT NEWS FILTER TEST ===")
    print("\n1. Makro Risk:")
    risky, score, words = analyze_macro_risk()
    print(f"   Riskli: {risky} | Skor: {score}/10 | Tetikleyiciler: {words}")

    for coin in ["BTC/USDT:USDT", "ETH/USDT:USDT", "BNB/USDT:USDT"]:
        r = analyze_coin_news(coin)
        print(f"\n2. {coin}: Risk={r['risk']} | Duygu={r['sentiment']} | Skor={r['score']}")
        if r['news']:
            print(f"   Haberler: {r['news']}")
