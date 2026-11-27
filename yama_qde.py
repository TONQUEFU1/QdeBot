from pathlib import Path

ee = Path("entry_engine.py")
if ee.exists():
    t = ee.read_text(encoding="utf-8")
    # Eşikleri esnet
    t = t.replace("ENTRY_QUALITY_THRESHOLD = 75.0", "ENTRY_QUALITY_THRESHOLD = 60.0")
    t = t.replace("MAX_STOP_DISTANCE_PCT = 8.0", "MAX_STOP_DISTANCE_PCT = 12.0")
    t = t.replace("MIN_RR_TP1 = 1.3", "MIN_RR_TP1 = 1.2")
    t = t.replace("tolerance_pct: float = 1.5", "tolerance_pct: float = 2.5")
    
    # Stop Loss akıllandırması: Dip çok uzaktaysa kırılım tamponunu stop al, işlemi öldürme
    old_sl = "stop_loss = min(sl_candidates) if sl_candidates else reference_entry * 0.92"
    new_sl = """valid_sls = [s for s in sl_candidates if (reference_entry - s) / reference_entry * 100 <= MAX_STOP_DISTANCE_PCT]
    stop_loss = min(valid_sls) if valid_sls else (max(sl_candidates) if sl_candidates else reference_entry * 0.95)"""
    
    if old_sl in t:
        t = t.replace(old_sl, new_sl)
        
    ee.write_text(t, encoding="utf-8")
    print("\n[OK] qde/entry_engine.py basariyla guncellendi!\n")
else:
    print("\n[-] entry_engine.py bulunamadi!\n")


