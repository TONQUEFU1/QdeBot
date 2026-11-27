"""
QDE V1.1 - tests/test_entry_engine.py

Regression tests for entry_engine.py (standalone Entry Engine plugin -
re-evaluates the already-generated Top15 for a single best LONG entry;
never modifies QDE's own scan/scoring/ranking).

Includes a specific regression test for a real bug found during manual
verification: the original find_recent_high()/detect_breakout()
implementation could fold the breakout candle's own high into the
"prior structural high" it was being compared against, whenever the
breakout happened a few candles before the most recent one (a normal,
expected situation once a retest phase follows the breakout).
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import pytest

from entry_engine import (
    Candle,
    EntryQualityBreakdown,
    Top15Candidate,
    analyze_technicals,
    build_trade_plan,
    compute_atr,
    compute_extension,
    detect_breakout,
    detect_retest,
    find_recent_high,
    find_swing_low,
    format_no_signal,
    format_signal,
    score_entry_quality,
    ENTRY_QUALITY_THRESHOLD,
    MAX_STOP_DISTANCE_PCT,
)
from models.result import ExpectedMove, Result
from models.setup import Setup


def _clean_breakout_retest_candles(seed: int = 1, base: float = 100.0) -> list[Candle]:
    """A consolidation, followed by a confirmed close-above-structure
    breakout, followed by a pullback that retests the breakout level
    and holds - the exact scenario the whole KIRILIM -> RETEST -> ONAY
    flow is meant to recognize.
    """
    candles: list[Candle] = []
    ts = 1_700_000_000_000
    random.seed(seed)
    for i in range(15):
        price = base + random.uniform(-1, 4)
        candles.append(Candle(timestamp=ts + i * 3600000, open=price, high=price + 1.5, low=price - 1.5, close=price, volume=1000))
    candles.append(Candle(timestamp=ts + 15 * 3600000, open=base + 4, high=base + 9, low=base + 3.5, close=base + 8, volume=3000))
    candles.append(Candle(timestamp=ts + 16 * 3600000, open=base + 8, high=base + 8.5, low=base + 5.2, close=base + 6, volume=1500))
    candles.append(Candle(timestamp=ts + 17 * 3600000, open=base + 6, high=base + 7, low=base + 5.5, close=base + 6.5, volume=1200))
    candles.append(Candle(timestamp=ts + 18 * 3600000, open=base + 6.5, high=base + 8, low=base + 6, close=base + 7.5, volume=1300))
    return candles


def _chased_breakout_candles(seed: int = 2, base: float = 100.0, chase_atr_multiples: float = 3.5) -> list[Candle]:
    """A breakout with NO retest - price runs straight away, well
    beyond MAX_EXTENSION_ATR_MULTIPLES - the "don't chase it" case.
    """
    candles: list[Candle] = []
    ts = 1_700_000_000_000
    random.seed(seed)
    for i in range(15):
        price = base + random.uniform(-1, 3)
        candles.append(Candle(timestamp=ts + i * 3600000, open=price, high=price + 1.2, low=price - 1.2, close=price, volume=1000))
    candles.append(Candle(timestamp=ts + 15 * 3600000, open=base + 3, high=base + 7, low=base + 2.5, close=base + 6, volume=2500))
    chase_price = base + 6 + chase_atr_multiples * 3.0
    candles.append(Candle(timestamp=ts + 16 * 3600000, open=base + 7, high=chase_price + 1, low=base + 6.7, close=chase_price, volume=2000))
    return candles


def _flat_no_breakout_candles(seed: int = 3, base: float = 10.0) -> list[Candle]:
    candles: list[Candle] = []
    ts = 1_700_000_000_000
    random.seed(seed)
    for i in range(20):
        price = base + random.uniform(-1, 1)
        candles.append(Candle(timestamp=ts + i * 3600000, open=price, high=price + 0.5, low=price - 0.5, close=price, volume=1000))
    return candles


def _make_candidate(symbol: str, market_regime: str = "bull", contradictions=None) -> Top15Candidate:
    return Top15Candidate(
        symbol=symbol, scan_id="test-scan", rank=1, qde_score=95.0, confidence=92.0, grade="A",
        price_at_scan=100.0, feature_scores={"trend": 15, "momentum": 12, "volume": 14},
        market_regime=market_regime, contradictions=contradictions or [],
    )


# ---------------------------------------------------------------------
# Pure technical analysis
# ---------------------------------------------------------------------

def test_compute_atr_requires_enough_candles():
    assert compute_atr([Candle(0, 1, 1, 1, 1, 1)] * 5, period=14) is None


def test_find_swing_low_uses_lowest_low_in_window():
    candles = [Candle(i, 10, 12, 8, 10, 100) for i in range(10)]
    candles[5] = Candle(5, 10, 12, 3, 10, 100)  # a clear lowest low
    assert find_swing_low(candles, lookback=10) == 3


def test_breakout_candle_does_not_contaminate_its_own_structural_high():
    """Regression test for the exact bug found during manual
    verification: find_recent_high(end_index=...) must exclude the
    breakout candle and everything after it, not just the single most
    recent candle in the whole series.
    """
    candles = _clean_breakout_retest_candles()
    confirmed, level, index = detect_breakout(candles)
    assert confirmed is True
    assert index == 15  # the actual breakout candle in the fixture
    # The detected level must come from the CONSOLIDATION range (roughly
    # base .. base+5), never from the breakout candle's own high (~109)
    # or the post-breakout pullback candles' highs.
    assert 99 < level < 106, f"breakout level {level} leaked data from the breakout candle itself"


def test_detect_retest_true_for_genuine_pullback_and_hold():
    candles = _clean_breakout_retest_candles()
    confirmed, level, index = detect_breakout(candles)
    assert detect_retest(candles, level, index) is True


def test_detect_retest_false_when_price_never_returns():
    candles = _chased_breakout_candles()
    confirmed, level, index = detect_breakout(candles)
    assert confirmed is True
    assert detect_retest(candles, level, index) is False


def test_detect_breakout_false_on_flat_range():
    candles = _flat_no_breakout_candles()
    confirmed, level, index = detect_breakout(candles)
    assert confirmed is False


def test_compute_extension_positive_when_price_above_breakout_level():
    ext = compute_extension(current_price=110.0, breakout_level=100.0, atr=5.0)
    assert ext == 2.0


# ---------------------------------------------------------------------
# Entry Quality scoring - the core "don't chase it" requirement
# ---------------------------------------------------------------------

def test_clean_breakout_retest_passes_threshold():
    """Previously relied on the old market_regime='bull' proxy to
    auto-grant +10 HTF points regardless of the coin's own structure.
    HTF Alignment V2 correctly no longer does that - this test now
    supplies a realistic, genuinely-aligned HTF result instead, since
    a "clean setup" in the real world would have real 1H/4H alignment
    backing it, not a free market-wide bonus.
    """
    from entry_engine import HTFAlignmentResult

    candles = _clean_breakout_retest_candles()
    tech = analyze_technicals(candles)
    candidate = _make_candidate("CLEANUSDT")
    aligned_htf = HTFAlignmentResult(
        trend_1h_score=4.0, trend_1h_label="BULLISH",
        structure_1h_score=3.0, structure_1h_label="BULLISH",
        context_4h_score=3.0, context_4h_label="BULLISH",
        points_earned=10.0, points_possible=10.0, status="FULL",
    )
    breakdown = score_entry_quality(candidate, tech, htf=aligned_htf)
    assert breakdown.total >= ENTRY_QUALITY_THRESHOLD


def test_chased_breakout_fails_threshold():
    candles = _chased_breakout_candles()
    tech = analyze_technicals(candles)
    candidate = _make_candidate("CHASEDUSDT")
    breakdown = score_entry_quality(candidate, tech)
    assert breakdown.total < ENTRY_QUALITY_THRESHOLD


def test_clean_setup_scores_higher_than_chased_setup():
    clean_tech = analyze_technicals(_clean_breakout_retest_candles())
    chased_tech = analyze_technicals(_chased_breakout_candles())
    clean_score = score_entry_quality(_make_candidate("A"), clean_tech).total
    chased_score = score_entry_quality(_make_candidate("B"), chased_tech).total
    assert clean_score > chased_score


def test_contradictions_reduce_entry_quality():
    candles = _clean_breakout_retest_candles()
    tech = analyze_technicals(candles)
    clean_candidate = _make_candidate("CLEAN", contradictions=[])
    contradicted_candidate = _make_candidate("CONTRA", contradictions=["conflicting_signals_within_trend"])
    clean_score = score_entry_quality(clean_candidate, tech).total
    contra_score = score_entry_quality(contradicted_candidate, tech).total
    assert contra_score < clean_score


def test_no_data_produces_zero_score_not_a_crash():
    breakdown = score_entry_quality(_make_candidate("NODATA"), tech=None)
    assert breakdown.total == 0.0


def test_breakdown_is_explainable_dict():
    candles = _clean_breakout_retest_candles()
    tech = analyze_technicals(candles)
    breakdown = score_entry_quality(_make_candidate("X"), tech)
    d = breakdown.as_dict()
    assert set(d.keys()) == {
        "breakout", "retest", "extension_penalty", "volume", "trend_momentum",
        "higher_timeframe", "contradiction_penalty", "liquidity_bonus", "total",
        "structural_penalty", "structural_status", "structural_ath", "structural_ath_distance_pct",
        "htf_trend_1h", "htf_structure_1h", "htf_context_4h", "htf_points", "htf_status",
    }


# ---------------------------------------------------------------------
# Trade plan (SL / TP / RR / leverage)
# ---------------------------------------------------------------------

def test_valid_trade_plan_has_correct_ordering():
    # Tighter consolidation so the resulting stop distance stays under
    # MAX_STOP_DISTANCE_PCT - see the module docstring's own note on
    # this exact edge case found during manual verification.
    candles = []
    ts = 1_700_000_000_000
    random.seed(3)
    for i in range(15):
        price = 100 + random.uniform(-0.5, 1.5)
        candles.append(Candle(timestamp=ts + i * 3600000, open=price, high=price + 0.6, low=price - 0.6, close=price, volume=1000))
    candles.append(Candle(timestamp=ts + 15 * 3600000, open=101, high=104, low=100.7, close=103.5, volume=2800))
    candles.append(Candle(timestamp=ts + 16 * 3600000, open=103.5, high=103.8, low=101.8, close=102.5, volume=1400))
    candles.append(Candle(timestamp=ts + 17 * 3600000, open=102.5, high=103.2, low=102, close=102.8, volume=1100))

    tech = analyze_technicals(candles)
    plan = build_trade_plan(tech, candles)

    assert plan.valid is True
    assert plan.stop_loss < plan.reference_entry < plan.tp1 < plan.tp2 < plan.tp3
    assert plan.rr["tp1"] < plan.rr["tp2"] < plan.rr["tp3"]
    assert 1.0 <= plan.suggested_leverage <= 10.0


def test_excessive_stop_distance_is_rejected():
    candles = _clean_breakout_retest_candles()  # wider consolidation -> wider stop
    tech = analyze_technicals(candles)
    plan = build_trade_plan(tech, candles)
    if not plan.valid:
        assert "Stop mesafesi" in plan.rejection_reason
    # If it happened to be valid, the stop distance must still respect the cap.
    else:
        stop_distance_pct = (plan.reference_entry - plan.stop_loss) / plan.reference_entry * 100
        assert stop_distance_pct <= MAX_STOP_DISTANCE_PCT


# ---------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------

def test_format_no_signal_contains_islem_yok():
    output = format_no_signal()
    assert "İŞLEM YOK" in output


def test_format_signal_contains_all_required_fields():
    from entry_engine import EntrySignal

    signal = EntrySignal(
        symbol="TESTUSDT", scan_id="s1", direction="LONG", qde_score=95.0, confidence=92.0,
        entry_quality=85.0, entry_zone=(1.0, 1.05), reference_entry=1.02, stop_loss=0.95,
        tp1=1.1, tp2=1.2, tp3=1.35, risk_label="DÜŞÜK", rr={"tp1": 1.8, "tp2": 3.0, "tp3": 4.5},
        suggested_leverage=4.0, breakout_confirmed=True, retest_confirmed=True,
        volume_confirmed=True, higher_tf_aligned=True, breakdown=EntryQualityBreakdown(),
    )
    output = format_signal(signal)
    for required in ["Coin", "Yön", "QDE Skoru", "Güven", "Giriş Kalitesi", "Giriş Bölgesi",
                      "Referans Giriş", "STOP LOSS", "TP1", "TP2", "TP3", "Risk", "Risk/Getiri",
                      "Önerilen Kaldıraç", "Kırılım", "Retest", "Hacim",
                      "1H Trend", "1H Yapı", "4H Bağlam", "HTF Puanı", "HTF Durumu",
                      "Sinyal Durumu"]:
        assert required in output, f"missing field: {required}"


# ---------------------------------------------------------------------
# Structural Health (365D ATH distance) - approved specification
# ---------------------------------------------------------------------

from entry_engine import (
    _structural_penalty_curve,
    _structural_status_label,
    analyze_structural_health,
    STRUCTURAL_HEALTH_MIN_DAYS,
)


@pytest.mark.parametrize("distance_pct,expected_penalty", [
    (49.9, 0.0),
    (50.0, 0.0),
    (65.0, 5.0),
    (80.0, 10.0),
    (85.0, 15.0),
    (90.0, 20.0),
    (95.0, 30.0),
    (100.0, 40.0),
])
def test_structural_penalty_curve_exact_boundaries(distance_pct, expected_penalty):
    actual = _structural_penalty_curve(distance_pct)
    assert abs(actual - expected_penalty) < 0.01, (
        f"distance={distance_pct}%: expected penalty {expected_penalty}, got {actual}"
    )


def test_structural_penalty_curve_98_3_percent_is_close_to_estimate():
    """The user's own worked example (PTB, real data) estimated
    "yaklaşık -38.3" at -98.3% - the precise linear-interpolation
    formula (consistent with every OTHER exact boundary above) gives
    36.6, a reasonable/expected difference from a rough manual
    estimate, not a bug. Asserted with a generous tolerance, not exact
    equality, since the user themselves flagged this one as approximate.
    """
    actual = _structural_penalty_curve(98.3)
    assert 34.0 <= actual <= 40.0


def test_structural_penalty_never_exceeds_the_40_point_cap():
    for distance in [100.0, 150.0, 1000.0]:
        assert _structural_penalty_curve(distance) <= 40.0


def test_structural_penalty_is_zero_below_50_percent_drawdown():
    for distance in [0.0, 10.0, 25.0, 49.0]:
        assert _structural_penalty_curve(distance) == 0.0


def test_structural_status_labels_match_penalty_bands():
    assert _structural_status_label(-30.0) == "SAGLIKLI"
    assert _structural_status_label(-65.0) == "DIKKAT"
    assert _structural_status_label(-85.0) == "YUKSEK_RISK"
    assert _structural_status_label(-98.3) == "COK_YUKSEK_RISK"


def test_insufficient_history_returns_zero_penalty_not_a_guess():
    short_candles = [
        Candle(timestamp=i * 86400000, open=1, high=1, low=1, close=1, volume=100)
        for i in range(100)  # well under the 365-day requirement
    ]
    result = analyze_structural_health(short_candles, current_price=1.0)
    assert result.status == "VERI_YETERSIZ"
    assert result.penalty == 0.0
    assert result.ath is None
    assert result.ath_distance_pct is None


def test_empty_candle_list_returns_insufficient_data():
    result = analyze_structural_health([], current_price=1.0)
    assert result.status == "VERI_YETERSIZ"
    assert result.penalty == 0.0


def test_structural_health_no_hard_reject_even_at_maximum_drawdown():
    """Core requirement: no matter how severe the drawdown, this is a
    penalty, never a hard rejection - the function always returns a
    usable result with a bounded penalty, never raises or signals
    disqualification on its own.
    """
    now_ms = 1_700_000_000_000
    day_ms = 86400000
    candles = [
        Candle(timestamp=now_ms - (366 - i) * day_ms, open=1, high=10.0, low=0.9, close=1.0, volume=100)
        for i in range(365)
    ]
    result = analyze_structural_health(candles, current_price=0.001)  # ~99.99% below ATH
    assert result.status == "COK_YUKSEK_RISK"
    assert 35.0 <= result.penalty <= 40.0  # bounded/capped, not infinite/unbounded
    assert result.ath == 10.0  # still a valid, usable result

    exact_cap = analyze_structural_health(candles, current_price=0.0000001)
    assert exact_cap.penalty == 40.0  # at the true 100% limit, the cap is reached exactly


def test_ath_computed_only_from_closed_candles_not_an_unclosed_one():
    """CRITICAL look-ahead regression test (the exact scenario the user
    specifically requested): ATH must come only from genuinely closed
    daily candles. This test proves the opposite failure mode would be
    severe - if an unclosed "today" candle with an inflated high leaked
    into the window, ATH (and therefore the entire structural health
    reading) would be silently wrong.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    day_ms = 86400000
    closed_candles = [
        Candle(timestamp=now_ms - (365 - i) * day_ms, open=1, high=1.0 + i * 0.001, low=0.9, close=1.0, volume=100)
        for i in range(365)
    ]
    true_ath = max(c.high for c in closed_candles)

    result = analyze_structural_health(closed_candles, current_price=0.5)
    assert abs(result.ath - true_ath) < 1e-9

    # Demonstrate the failure mode this guards against: an unclosed
    # candle with an absurd high WOULD corrupt the ATH if it leaked in -
    # this is exactly why fetch_top15_candles applies
    # _drop_unclosed_candle before analyze_structural_health ever runs.
    leaked = closed_candles + [Candle(timestamp=now_ms, open=1, high=999.0, low=1, close=1, volume=100)]
    corrupted_result = analyze_structural_health(leaked, current_price=0.5)
    assert corrupted_result.ath == 999.0, "sanity check: confirms the leak scenario itself is set up correctly"
    assert corrupted_result.ath != result.ath, (
        "an unclosed candle changing the ATH is exactly the bug fetch_top15_candles's "
        "_drop_unclosed_candle call must prevent in the real fetch path"
    )


async def test_fetch_top15_candles_drops_unclosed_candle_for_any_timeframe():
    """Integration-level confirmation that the real fetch path (not
    just analyze_structural_health in isolation) applies the look-ahead
    fix for the 1d timeframe used by Structural Health, exactly as it
    already does for 1h.
    """
    from unittest.mock import AsyncMock
    import entry_engine as ee

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    day_ms = 86400000
    limit = 10
    raw_rows = [[now_ms - (limit - i) * day_ms, 1, 1.1, 0.9, 1.0, 100] for i in range(limit)]
    raw_rows[-1] = [now_ms - day_ms // 3, 1, 1.1, 0.9, 1.0, 100]  # today's candle, still forming

    fake_exchange = AsyncMock()
    fake_exchange.get_ohlcv = AsyncMock(return_value=raw_rows)
    fake_exchange.close = AsyncMock()

    import data.exchange as de
    original_cls = de.ExchangeClient
    de.ExchangeClient = lambda rate_limit_config: fake_exchange
    try:
        result = await ee.fetch_top15_candles(["XUSDT"], timeframe="1d", limit=limit)
    finally:
        de.ExchangeClient = original_cls

    assert len(result["XUSDT"]) == limit - 1, "the unclosed daily candle must be dropped"


def test_score_entry_quality_applies_structural_penalty_independently_of_technical_score():
    """Structural Health must apply even when it's the ONLY signal
    available (e.g. technical analysis found nothing noteworthy) -
    confirms the two components are properly decoupled, per the
    approved "separate, testable component" requirement.
    """
    from entry_engine import StructuralHealthResult, score_entry_quality

    candidate = _make_candidate("DRAWDOWNUSDT")
    severe_structural = StructuralHealthResult(
        status="COK_YUKSEK_RISK", ath=1.0, ath_distance_pct=-98.3, penalty=36.6, candles_available=365,
    )
    breakdown = score_entry_quality(candidate, tech=None, structural=severe_structural)
    assert breakdown.structural_penalty == 36.6
    assert breakdown.structural_status == "COK_YUKSEK_RISK"
    assert breakdown.structural_ath_distance_pct == -98.3


def test_structural_penalty_reduces_total_entry_quality():
    candles = _clean_breakout_retest_candles()
    tech = analyze_technicals(candles)
    candidate = _make_candidate("X")

    from entry_engine import StructuralHealthResult, score_entry_quality

    healthy = StructuralHealthResult(status="SAGLIKLI", ath=200.0, ath_distance_pct=-10.0, penalty=0.0, candles_available=365)
    severe = StructuralHealthResult(status="COK_YUKSEK_RISK", ath=200.0, ath_distance_pct=-98.0, penalty=36.0, candles_available=365)

    score_healthy = score_entry_quality(candidate, tech, healthy).total
    score_severe = score_entry_quality(candidate, tech, severe).total
    assert score_severe == pytest.approx(score_healthy - 36.0, abs=0.1)


# ---------------------------------------------------------------------
# Breakout Detector V2 - approved architecture: BREAKOUT_SEARCH_WINDOW
# (how far back the breakout event may have occurred) and
# RETEST_VALIDITY_WINDOW (how long after it a retest still counts) are
# two separate, independently-configurable concepts, never conflated.
# Found and fixed a real off-by-one while building the exact boundary
# matrix specified by the user: a breakout exactly
# BREAKOUT_SEARCH_WINDOW candles ago must be found, one candle further
# back must not be.
# ---------------------------------------------------------------------

from entry_engine import BREAKOUT_SEARCH_WINDOW, RETEST_VALIDITY_WINDOW


def _candles_with_breakout_at(total: int, candles_ago: int, base: float = 100.0, flat_after: bool = True) -> tuple[list[Candle], int]:
    """Builds `total` 1h candles with a real, close-confirmed breakout
    exactly `candles_ago` candles before the last one. Candles after
    the breakout are flat/neutral by default (no accidental
    re-breakout contaminating the test).
    """
    candles = []
    for i in range(total):
        price = base + (i % 3) * 0.1  # mild consolidation noise, never breaking base+5
        candles.append(Candle(timestamp=i, open=price, high=price + 0.3, low=price - 0.3, close=price, volume=100))
    breakout_idx = total - 1 - candles_ago
    candles[breakout_idx] = Candle(timestamp=breakout_idx, open=base, high=base + 5, low=base - 0.3, close=base + 4, volume=500)
    if flat_after:
        for j in range(breakout_idx + 1, total):
            candles[j] = Candle(timestamp=j, open=base + 4, high=base + 4.3, low=base + 3.7, close=base + 4, volume=100)
    return candles, breakout_idx


@pytest.mark.parametrize("candles_ago", [5, 10, 16])
def test_breakout_found_within_search_window(candles_ago):
    candles, expected_idx = _candles_with_breakout_at(60, candles_ago)
    confirmed, level, idx = detect_breakout(candles)
    assert confirmed is True
    assert idx == expected_idx


def test_breakout_not_found_one_candle_beyond_search_window():
    candles, _ = _candles_with_breakout_at(60, BREAKOUT_SEARCH_WINDOW + 1)
    confirmed, level, idx = detect_breakout(candles)
    assert confirmed is False


def test_breakout_search_window_boundary_is_inclusive():
    """candles_ago == BREAKOUT_SEARCH_WINDOW exactly must be found -
    the off-by-one this test guards against was caught by this exact
    boundary case during manual verification.
    """
    candles, expected_idx = _candles_with_breakout_at(60, BREAKOUT_SEARCH_WINDOW)
    confirmed, level, idx = detect_breakout(candles)
    assert confirmed is True
    assert idx == expected_idx


def test_retest_valid_within_retest_window():
    candles, breakout_idx = _candles_with_breakout_at(60, 10, flat_after=False)
    # First find the REAL level (the highest high in the consolidation
    # window) rather than assuming the breakout candle's own close -
    # find_recent_high looks at highs, not closes, of the prior window.
    confirmed_before, level, idx_before = detect_breakout(candles)
    assert confirmed_before is True
    # Now place a genuine retest-and-hold inside the retest window,
    # anchored to the REAL level just discovered.
    for j in range(breakout_idx + 1, breakout_idx + 1 + RETEST_VALIDITY_WINDOW):
        candles[j] = Candle(timestamp=j, open=level, high=level + 0.2, low=level - 0.3, close=level, volume=100)
    for j in range(breakout_idx + 1 + RETEST_VALIDITY_WINDOW, 60):
        candles[j] = Candle(timestamp=j, open=level, high=level + 0.5, low=level, close=level + 0.3, volume=100)
    confirmed, found_level, idx = detect_breakout(candles)
    assert confirmed is True
    assert detect_retest(candles, found_level, idx) is True


def test_retest_invalid_when_no_pullback_ever_occurs():
    candles, breakout_idx = _candles_with_breakout_at(60, 10)  # flat_after=True, price never comes back near the level
    confirmed, found_level, idx = detect_breakout(candles)
    assert confirmed is True
    assert detect_retest(candles, found_level, idx) is False


def test_retest_invalid_when_pullback_occurs_after_validity_window_expires():
    """'Kırılım var ama retest süresi dolmuşken' - a pullback DOES
    happen in the data, but only after RETEST_VALIDITY_WINDOW candles
    have already passed since the breakout - must NOT count as a
    valid retest, since detect_retest only inspects the fixed window
    immediately after the breakout, never the full remaining array.
    """
    # 14 candles-ago: within BREAKOUT_SEARCH_WINDOW(16) so the breakout
    # itself is still found, while leaving enough trailing candles
    # (13) for both the full retest window (6) and a late pullback
    # a couple of candles after it expires.
    candles, breakout_idx = _candles_with_breakout_at(60, 14, flat_after=False)
    confirmed_before, level, _ = detect_breakout(candles)
    assert confirmed_before is True

    # Keep price away from the level during the valid window...
    for j in range(breakout_idx + 1, breakout_idx + 1 + RETEST_VALIDITY_WINDOW):
        candles[j] = Candle(timestamp=j, open=level + 3.5, high=level + 4, low=level + 3, close=level + 3.5, volume=100)
    # ...then pull back near the level only AFTER the window has expired.
    late_pullback_idx = breakout_idx + 1 + RETEST_VALIDITY_WINDOW + 2
    candles[late_pullback_idx] = Candle(timestamp=late_pullback_idx, open=level + 2, high=level + 2, low=level - 0.1, close=level + 1, volume=100)
    for j in range(late_pullback_idx + 1, 60):
        candles[j] = Candle(timestamp=j, open=level + 3.5, high=level + 4, low=level + 3, close=level + 3.5, volume=100)

    confirmed, found_level, idx = detect_breakout(candles)
    assert confirmed is True
    assert detect_retest(candles, found_level, idx) is False, (
        "a pullback occurring after RETEST_VALIDITY_WINDOW has expired must not count as a valid retest"
    )


def test_unclosed_last_candle_not_used_for_breakout_or_retest_decision():
    """'Son mum henüz kapanmamışken' - this is the same
    _drop_unclosed_candle protection already applied at the fetch
    layer (fetch_top15_candles). This test demonstrates WHY that
    protection matters for retest validity specifically: the unclosed
    candle must sit at the very end of the active retest window
    (representing "now", mid-formation) for its still-moving price to
    be capable of corrupting the held_above check - an unclosed candle
    appended well past an already-closed retest window changes nothing
    (confirmed as a dead end during manual verification), which is
    itself informative: contamination only matters at the live edge.
    """
    # Breakout exactly RETEST_VALIDITY_WINDOW candles before the end,
    # so the retest window's LAST slot (breakout_idx+RETEST_VALIDITY_WINDOW)
    # lands exactly on the array's true final index (total-1).
    total = 59
    candles_ago = RETEST_VALIDITY_WINDOW
    candles, breakout_idx = _candles_with_breakout_at(total, candles_ago, flat_after=False)
    assert breakout_idx + RETEST_VALIDITY_WINDOW == total - 1, "test index arithmetic sanity check"
    confirmed_probe, level, _ = detect_breakout(candles)
    assert confirmed_probe is True

    # Fill the retest window with a genuine pullback-and-hold, EXCEPT
    # the very last candle, which is still "forming" - its true,
    # eventually-closed low will come back near the level and close
    # back above it (a valid retest), but RIGHT NOW (unclosed) it's
    # showing a much lower, still-moving close.
    window_end = breakout_idx + 1 + RETEST_VALIDITY_WINDOW
    for j in range(breakout_idx + 1, window_end - 1):
        candles[j] = Candle(timestamp=j, open=level, high=level + 0.2, low=level - 0.3, close=level, volume=100)
    true_closed_final_candle = Candle(timestamp=window_end - 1, open=level, high=level + 0.2, low=level - 0.3, close=level, volume=100)
    unclosed_version_of_final_candle = Candle(timestamp=window_end - 1, open=level, high=level + 0.1, low=level - 5.0, close=level - 5.0, volume=50)

    candles_with_closed_final = candles[:-1] + [true_closed_final_candle]
    candles_with_unclosed_final = candles[:-1] + [unclosed_version_of_final_candle]

    confirmed_closed, found_level, idx = detect_breakout(candles_with_closed_final)
    retest_closed = detect_retest(candles_with_closed_final, found_level, idx)
    assert confirmed_closed is True and retest_closed is True, "with the true closed candle, this should be a valid retest"

    confirmed_unclosed, found_level2, idx2 = detect_breakout(candles_with_unclosed_final)
    retest_unclosed = detect_retest(candles_with_unclosed_final, found_level2, idx2)
    assert retest_unclosed is False, (
        "sanity check: confirms an unclosed final candle, if it leaked in, WOULD corrupt the "
        "retest decision - exactly why fetch_top15_candles must drop it before these functions ever see it"
    )


def test_breakout_and_retest_windows_are_independently_configurable():
    """Confirms the two windows don't secretly share state or defaults
    - passing a custom retest_window changes retest validity without
    touching the breakout search window, and vice versa.
    """
    candles, breakout_idx = _candles_with_breakout_at(60, 10, flat_after=False)
    level = 100.0 + 4.0
    # Pullback at candle breakout_idx+8 - outside the DEFAULT retest
    # window (6) but inside a wider custom one (10).
    late_idx = breakout_idx + 8
    for j in range(breakout_idx + 1, 60):
        candles[j] = Candle(timestamp=j, open=level + 3.5, high=level + 4, low=level + 3, close=level + 3.5, volume=100)
    candles[late_idx] = Candle(timestamp=late_idx, open=level + 2, high=level + 2, low=level - 0.1, close=level + 1.5, volume=100)

    assert detect_retest(candles, level, breakout_idx, retest_window=6) is False
    assert detect_retest(candles, level, breakout_idx, retest_window=10) is True


def test_same_ohlcv_data_produces_identical_results_every_call():
    """Determinism check (approved test requirement #6): no hidden
    state, no randomness - identical inputs must always produce
    identical outputs.
    """
    candles, _ = _candles_with_breakout_at(60, 10, flat_after=False)
    results = [detect_breakout(candles) for _ in range(5)]
    assert all(r == results[0] for r in results)


# ---------------------------------------------------------------------
# HTF Alignment V2 - approved architecture: real, coin-specific 1H
# trend + 1H structure + 4H context, replacing the old market_regime
# proxy entirely. Score/status/reason kept separate so insufficient
# data is never silently read as bearish, and a PARTIAL result is
# never rescaled to hide a missing component.
# ---------------------------------------------------------------------

from entry_engine import (
    HTFAlignmentResult,
    analyze_htf_alignment,
    compute_ema,
    HTF_TREND_1H_MAX,
    HTF_STRUCTURE_1H_MAX,
    HTF_CONTEXT_4H_MAX,
    HTF_EMA_FAST_PERIOD,
    HTF_EMA_SLOW_PERIOD,
    HTF_STRUCTURE_LOOKBACK,
)


def _flat_candles(n: int, price: float = 100.0) -> list[Candle]:
    return [Candle(timestamp=i, open=price, high=price + 0.1, low=price - 0.1, close=price, volume=100) for i in range(n)]


def _rising_candles(n: int, start: float = 100.0, step: float = 0.5) -> list[Candle]:
    candles = []
    for i in range(n):
        price = start + i * step
        candles.append(Candle(timestamp=i, open=price, high=price + 0.3, low=price - 0.3, close=price, volume=100))
    return candles


def _falling_candles(n: int, start: float = 150.0, step: float = 0.5) -> list[Candle]:
    candles = []
    for i in range(n):
        price = start - i * step
        candles.append(Candle(timestamp=i, open=price, high=price + 0.3, low=price - 0.3, close=price, volume=100))
    return candles


def test_compute_ema_known_values():
    # Simple, hand-verifiable case: constant price -> EMA equals that price.
    closes = [100.0] * 60
    assert compute_ema(closes, 20) == pytest.approx(100.0)
    assert compute_ema(closes, 50) == pytest.approx(100.0)


def test_compute_ema_insufficient_data_returns_none():
    assert compute_ema([100.0] * 10, 20) is None


def test_compute_ema_reacts_to_trend():
    rising = [100.0 + i * 0.5 for i in range(60)]
    ema20 = compute_ema(rising, 20)
    ema50 = compute_ema(rising, 50)
    # On a steadily rising series, the faster EMA must sit above the slower one.
    assert ema20 > ema50


def test_htf_full_status_when_all_components_computable():
    candles_1h = _rising_candles(60)
    candles_4h = _rising_candles(60)
    result = analyze_htf_alignment(candles_1h, candles_4h, current_price=candles_1h[-1].close)
    assert result.status == "FULL"
    assert result.points_possible == HTF_TREND_1H_MAX + HTF_STRUCTURE_1H_MAX + HTF_CONTEXT_4H_MAX


def test_htf_partial_status_when_4h_missing():
    candles_1h = _rising_candles(60)
    candles_4h: list[Candle] = []  # e.g. a fetch failure for this symbol
    result = analyze_htf_alignment(candles_1h, candles_4h, current_price=candles_1h[-1].close)
    assert result.status == "PARTIAL"
    assert result.context_4h_score is None
    assert result.context_4h_label == "INSUFFICIENT_DATA"
    # Only the two computable components count toward points_possible.
    assert result.points_possible == HTF_TREND_1H_MAX + HTF_STRUCTURE_1H_MAX


def test_htf_partial_score_is_never_rescaled_to_hide_the_gap():
    """The user's own example: '5/7, PARTIAL' must contribute exactly
    5 points toward Entry Quality, never inflated to 5/7*10=7.14 to
    disguise that 4H was unavailable.
    """
    candles_1h = _rising_candles(60)
    result = analyze_htf_alignment(candles_1h, [], current_price=candles_1h[-1].close)
    assert result.status == "PARTIAL"
    assert result.points_earned <= result.points_possible
    # The raw points_earned is what score_entry_quality consumes directly (see wiring below).
    candidate = _make_candidate("PARTIALUSDT")
    breakdown = score_entry_quality(candidate, tech=None, htf=result)
    assert breakdown.higher_tf_score == result.points_earned  # NOT rescaled against points_possible


def test_htf_insufficient_data_status_when_nothing_computable():
    result = analyze_htf_alignment([], [], current_price=100.0)
    assert result.status == "INSUFFICIENT_DATA"
    assert result.points_earned == 0.0
    assert result.points_possible == 0.0
    assert result.trend_1h_label == "INSUFFICIENT_DATA"
    assert result.structure_1h_label == "INSUFFICIENT_DATA"
    assert result.context_4h_label == "INSUFFICIENT_DATA"


def test_htf_bullish_trend_scores_above_neutral_midpoint():
    candles_1h = _rising_candles(60)
    score, label = _ema_relationship_score_closes(candles_1h)
    assert label == "BULLISH"
    assert score > HTF_TREND_1H_MAX * 0.5


def test_htf_bearish_trend_scores_below_neutral_midpoint():
    candles_1h = _falling_candles(60)
    score, label = _ema_relationship_score_closes(candles_1h)
    assert label == "BEARISH"
    assert score < HTF_TREND_1H_MAX * 0.5


def test_htf_flat_market_scores_near_neutral_midpoint():
    candles_1h = _flat_candles(60)
    score, label = _ema_relationship_score_closes(candles_1h)
    assert label == "NEUTRAL"
    assert score == pytest.approx(HTF_TREND_1H_MAX * 0.5, abs=0.1)


def _ema_relationship_score_closes(candles_1h):
    from entry_engine import _ema_relationship_score
    return _ema_relationship_score([c.close for c in candles_1h], HTF_TREND_1H_MAX)


def test_htf_structure_score_reflects_position_in_range():
    from entry_engine import _structure_score

    candles = _flat_candles(HTF_STRUCTURE_LOOKBACK)
    # Widen the range explicitly: low end at 95, high end at 105.
    candles[0] = Candle(timestamp=0, open=100, high=100, low=95, close=100, volume=100)
    candles[-1] = Candle(timestamp=len(candles) - 1, open=100, high=105, low=100, close=100, volume=100)

    near_top_score, near_top_label = _structure_score(candles, current_price=104.0, max_points=HTF_STRUCTURE_1H_MAX)
    near_bottom_score, near_bottom_label = _structure_score(candles, current_price=96.0, max_points=HTF_STRUCTURE_1H_MAX)

    assert near_top_score > near_bottom_score
    assert near_top_label == "BULLISH"
    assert near_bottom_label == "BEARISH"


def test_htf_structure_insufficient_data_below_lookback():
    from entry_engine import _structure_score

    short = _flat_candles(HTF_STRUCTURE_LOOKBACK - 1)
    score, label = _structure_score(short, current_price=100.0, max_points=HTF_STRUCTURE_1H_MAX)
    assert score is None
    assert label == "INSUFFICIENT_DATA"


def test_htf_never_consults_market_regime():
    """Core architectural requirement: HTF Alignment V2 is entirely
    coin-specific. A candidate with market_regime='bear' but genuinely
    bullish 1H/4H candles must still score well - proving the two
    concepts are fully decoupled.
    """
    candidate_bear_regime = Top15Candidate(
        symbol="X", scan_id="s", rank=1, qde_score=90, confidence=90, grade="A",
        price_at_scan=100, feature_scores={}, market_regime="bear", contradictions=[],
    )
    candles_1h = _rising_candles(60)
    candles_4h = _rising_candles(60)
    htf = analyze_htf_alignment(candles_1h, candles_4h, current_price=candles_1h[-1].close)
    breakdown = score_entry_quality(candidate_bear_regime, tech=None, htf=htf)
    assert breakdown.higher_tf_score > HTF_TREND_1H_MAX + HTF_STRUCTURE_1H_MAX + HTF_CONTEXT_4H_MAX * 0.5, (
        "a genuinely bullish coin must score well regardless of the overall market_regime label"
    )


async def test_htf_4h_fetch_is_look_ahead_safe():
    """Confirms the 4H fetch used by HTF Alignment V2 goes through the
    same _drop_unclosed_candle protection as every other timeframe.
    """
    from unittest.mock import AsyncMock
    import entry_engine as ee

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    four_h_ms = 4 * 60 * 60 * 1000
    limit = 10
    raw_rows = [[now_ms - (limit - i) * four_h_ms, 100, 101, 99, 100, 100] for i in range(limit)]
    raw_rows[-1] = [now_ms - four_h_ms // 3, 100, 101, 99, 100, 100]  # still forming

    fake_exchange = AsyncMock()
    fake_exchange.get_ohlcv = AsyncMock(return_value=raw_rows)
    fake_exchange.close = AsyncMock()

    import data.exchange as de
    original_cls = de.ExchangeClient
    de.ExchangeClient = lambda rate_limit_config: fake_exchange
    try:
        result = await ee.fetch_top15_candles(["XUSDT"], timeframe="4h", limit=limit)
    finally:
        de.ExchangeClient = original_cls

    assert len(result["XUSDT"]) == limit - 1, "the unclosed 4h candle must be dropped"


# ---------------------------------------------------------------------
# entry_candidates logging + single-pass evaluation
# (logging only - nothing here may change what gets selected)
# ---------------------------------------------------------------------

import sqlite3

import entry_engine as ee


def _pattern_candles_60(base: float = 100.0) -> list[Candle]:
    """60 closed 1h candles ending in ONE unambiguous, close-confirmed
    breakout (candle 57) followed by a retest-and-hold (58, 59).

    The 57 candles before it are deterministic and quiet: no candle can
    close above the highest high of the 15 before it, so detect_breakout
    can only find the real breakout. (An earlier random-noise version
    occasionally printed a minor breakout of its own, and since the
    detector returns the EARLIEST breakout in its window, the fixture
    silently described a different setup than the one it was meant to.)
    """
    candles = []
    for i in range(57):
        price = base + (i % 3) * 0.1
        candles.append(Candle(timestamp=i, open=price, high=price + 0.3, low=price - 0.3, close=price, volume=1000))
    candles.append(Candle(timestamp=57, open=base + 0.2, high=base + 3.0, low=base + 0.1, close=base + 2.5, volume=2800))  # breakout
    candles.append(Candle(timestamp=58, open=base + 2.5, high=base + 2.6, low=base + 0.7, close=base + 1.0, volume=1400))  # retest
    candles.append(Candle(timestamp=59, open=base + 1.0, high=base + 1.3, low=base + 0.6, close=base + 0.9, volume=1100))
    return candles


def test_pattern_fixture_is_unambiguous():
    """Guards the fixture itself: exactly one breakout, at the intended
    candle, retested, and not extended.
    """
    candles = _pattern_candles_60()
    confirmed, level, idx = detect_breakout(candles)
    assert confirmed is True and idx == 57
    assert detect_retest(candles, level, idx) is True
    tech = analyze_technicals(candles)
    assert tech.breakout_candles_ago == 2
    assert tech.extension_atr_multiples < 1.0


def _daily_flat_365(price: float = 100.0, ath_factor: float = 1.2) -> list[Candle]:
    return [Candle(timestamp=i, open=price, high=price * ath_factor, low=price * 0.95, close=price, volume=100) for i in range(365)]


def _candidate(symbol: str, setup_id: str | None = None, rank: int = 1) -> Top15Candidate:
    return Top15Candidate(
        symbol=symbol, scan_id="scan-1", rank=rank, qde_score=100.0, confidence=100.0, grade="A",
        price_at_scan=102.0, feature_scores={"trend": 15, "momentum": 12, "volume": 14},
        market_regime="sideways", contradictions=[], setup_id=setup_id,
    )


def _install_fake_fetch(monkeypatch, calls: list[str]):
    """Replaces fetch_top15_candles with deterministic per-symbol data and
    records which timeframe each call asked for.
    """
    one_h = {
        "GOODUSDT": _pattern_candles_60(),
        "CHASEDUSDT": _chased_breakout_candles(),
        "NODATAUSDT": [],
    }

    async def fake_fetch(symbols, timeframe="1h", limit=60):
        calls.append(timeframe)
        if timeframe == "1h":
            return {s: one_h.get(s, []) for s in symbols}
        if timeframe == "1d":
            return {s: _daily_flat_365() for s in symbols}
        if timeframe == "4h":
            return {s: _rising_candles(60) for s in symbols}
        raise AssertionError(f"unexpected timeframe {timeframe}")

    monkeypatch.setattr(ee, "fetch_top15_candles", fake_fetch)


async def test_evaluate_top15_fetches_each_timeframe_exactly_once(monkeypatch):
    """run() and select_best_entry() used to each fetch everything,
    doubling API calls and letting the --explain table and the real
    decision come from two different snapshots.
    """
    calls: list[str] = []
    _install_fake_fetch(monkeypatch, calls)
    await ee.evaluate_top15([_candidate("GOODUSDT"), _candidate("CHASEDUSDT", rank=2)])
    assert sorted(calls) == ["1d", "1h", "4h"]


async def test_evaluate_top15_selects_the_clean_setup_and_marks_only_it(monkeypatch):
    _install_fake_fetch(monkeypatch, [])
    candidates = [_candidate("CHASEDUSDT", rank=1), _candidate("GOODUSDT", rank=2), _candidate("NODATAUSDT", rank=3)]
    signal, evals = await ee.evaluate_top15(candidates)

    assert signal is not None and signal.symbol == "GOODUSDT"
    assert [e.candidate.symbol for e in evals] == ["CHASEDUSDT", "GOODUSDT", "NODATAUSDT"]  # input order kept
    assert [e.selected for e in evals] == [False, True, False]


async def test_select_best_entry_wrapper_matches_evaluate_top15(monkeypatch):
    _install_fake_fetch(monkeypatch, [])
    candidates = [_candidate("GOODUSDT"), _candidate("CHASEDUSDT", rank=2)]
    signal_via_wrapper = await ee.select_best_entry(candidates)
    signal_direct, _ = await ee.evaluate_top15(candidates)
    assert signal_via_wrapper.symbol == signal_direct.symbol
    assert signal_via_wrapper.entry_quality == signal_direct.entry_quality


async def test_no_candidates_returns_nothing():
    signal, evals = await ee.evaluate_top15([])
    assert signal is None and evals == []


async def test_record_candidates_writes_every_candidate_not_only_the_selected(monkeypatch, tmp_path):
    monkeypatch.setattr(ee, "_SIGNAL_DB_PATH", tmp_path / "entry_signals.db")
    _install_fake_fetch(monkeypatch, [])
    candidates = [
        _candidate("CHASEDUSDT", setup_id="setup-chase", rank=1),
        _candidate("GOODUSDT", setup_id="setup-good", rank=2),
        _candidate("NODATAUSDT", setup_id="setup-nodata", rank=3),
    ]
    _, evals = await ee.evaluate_top15(candidates)
    written = ee.record_candidates("2026-11-01T00:00:00+00:00", evals)
    assert written == 3

    conn = sqlite3.connect(tmp_path / "entry_signals.db")
    conn.row_factory = sqlite3.Row
    rows = {r["symbol"]: r for r in conn.execute("SELECT * FROM entry_candidates")}
    conn.close()

    assert set(rows) == {"CHASEDUSDT", "GOODUSDT", "NODATAUSDT"}
    assert rows["GOODUSDT"]["selected"] == 1
    assert rows["CHASEDUSDT"]["selected"] == 0 and rows["NODATAUSDT"]["selected"] == 0
    # setup_id is what lets these rows be joined to outcome_engine's results
    assert rows["GOODUSDT"]["setup_id"] == "setup-good"
    # raw technical context needed to re-fit the bands later is stored, not just labels
    assert rows["GOODUSDT"]["breakout_confirmed"] == 1
    assert rows["GOODUSDT"]["atr"] is not None and rows["GOODUSDT"]["extension_atr"] is not None
    assert rows["GOODUSDT"]["breakout_candles_ago"] == 2  # how stale the reference level is, logged for later diagnosis
    assert rows["CHASEDUSDT"]["extension_atr"] > 2.0  # the chased one is recorded as chased
    # the stored breakdown is the one the decision used
    assert json.loads(rows["GOODUSDT"]["breakdown_json"])["total"] == rows["GOODUSDT"]["entry_quality"]


async def test_missing_technical_data_is_stored_as_null_not_zero(monkeypatch, tmp_path):
    """'Not computed' must stay distinguishable from 'zero' - a coin with
    no 1h data must not look like one with a flat 0.0 ATR / 0 extension.
    """
    monkeypatch.setattr(ee, "_SIGNAL_DB_PATH", tmp_path / "entry_signals.db")
    _install_fake_fetch(monkeypatch, [])
    _, evals = await ee.evaluate_top15([_candidate("NODATAUSDT", setup_id="s")])
    ee.record_candidates("t", evals)

    conn = sqlite3.connect(tmp_path / "entry_signals.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM entry_candidates").fetchone()
    conn.close()
    for column in ("breakout_confirmed", "retest_confirmed", "breakout_level", "atr", "extension_atr", "breakout_candles_ago", "swing_low", "candles_1h"):
        assert row[column] is None, f"{column} should be NULL when no 1h data was available"


def test_record_candidates_with_nothing_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "_SIGNAL_DB_PATH", tmp_path / "entry_signals.db")
    assert ee.record_candidates("t", []) == 0


async def test_run_logs_all_candidates_and_still_prints_the_result(monkeypatch, tmp_path):
    monkeypatch.setattr(ee, "_SIGNAL_DB_PATH", tmp_path / "entry_signals.db")
    _install_fake_fetch(monkeypatch, [])
    monkeypatch.setattr(ee, "fetch_latest_top15", lambda: [_candidate("GOODUSDT", "s1"), _candidate("CHASEDUSDT", "s2", rank=2)])

    output = await ee.run(explain=True)
    assert "QDE LONG" in output and "GOODUSDT" in output
    assert "Giris Kalitesi Detayi" in output

    conn = sqlite3.connect(tmp_path / "entry_signals.db")
    n = conn.execute("SELECT COUNT(*) FROM entry_candidates").fetchone()[0]
    selected = conn.execute("SELECT COUNT(*) FROM entry_candidates WHERE selected = 1").fetchone()[0]
    conn.close()
    assert n == 2 and selected == 1


async def test_run_logs_candidates_even_when_no_trade(monkeypatch, tmp_path):
    """The 'İŞLEM YOK' runs are exactly the ones whose breakdown used to be
    lost - they are the most important to keep.
    """
    monkeypatch.setattr(ee, "_SIGNAL_DB_PATH", tmp_path / "entry_signals.db")
    _install_fake_fetch(monkeypatch, [])
    monkeypatch.setattr(ee, "fetch_latest_top15", lambda: [_candidate("CHASEDUSDT", "s1"), _candidate("NODATAUSDT", "s2", rank=2)])

    output = await ee.run()
    assert "İŞLEM YOK" in output

    conn = sqlite3.connect(tmp_path / "entry_signals.db")
    rows = conn.execute("SELECT selected FROM entry_candidates").fetchall()
    conn.close()
    assert len(rows) == 2 and all(r[0] == 0 for r in rows)


async def test_logging_failure_never_blocks_output_but_is_never_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(ee, "_SIGNAL_DB_PATH", tmp_path / "entry_signals.db")
    _install_fake_fetch(monkeypatch, [])
    monkeypatch.setattr(ee, "fetch_latest_top15", lambda: [_candidate("GOODUSDT", "s1")])

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ee, "record_candidates", boom)
    output = await ee.run()
    assert "UYARI" in output and "disk I/O error" in output  # visible, not swallowed
    assert "QDE LONG" in output  # the signal itself is still delivered


def test_existing_entry_signals_db_migrates_without_losing_rows(tmp_path, monkeypatch):
    """A database created before entry_candidates existed must open
    cleanly, keep its old rows, and gain the new table.
    """
    db = tmp_path / "entry_signals.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE entry_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id TEXT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL, signal_status TEXT)")
    conn.execute("INSERT INTO entry_signals (scan_id, timestamp, symbol, direction, signal_status) VALUES ('old', 't', 'OLDUSDT', 'LONG', 'GİRİLEBİLİR')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(ee, "_SIGNAL_DB_PATH", db)
    conn = ee._connect_signal_db()
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        old = conn.execute("SELECT symbol FROM entry_signals").fetchall()
    finally:
        conn.close()
    assert "entry_candidates" in tables
    assert [r[0] for r in old] == ["OLDUSDT"]


def test_fetch_latest_top15_carries_setup_id(monkeypatch, tmp_path):
    import performance_tracker as pt

    monkeypatch.setattr(pt, "_DB_PATH", tmp_path / "performance.db")
    monkeypatch.setattr(ee, "_ROOT", tmp_path)  # enrichment opens <root>/qde.db; keep it out of the project dir
    conn = pt._connect()
    conn.execute(
        "INSERT INTO top15_snapshots (scan_id, scan_timestamp, rank, symbol, score, confidence, grade, category, price_at_scan, setup_id, feature_scores_json) "
        "VALUES ('scan-9', '2026-11-01T00:00:00+00:00', 1, 'GOODUSDT', 100, 100, 'A', 'High', 1.0, 'setup-abc', '{}')"
    )
    conn.commit()
    conn.close()

    candidates = ee.fetch_latest_top15()
    assert [c.setup_id for c in candidates] == ["setup-abc"]


def test_detect_breakout_returns_earliest_breakout_in_window_characterization():
    """CHARACTERIZATION (documents current behavior, does not endorse it):
    in a smooth uptrend every few candles close above their prior
    15-candle high, and the detector returns the EARLIEST of them - the
    reference level is then as stale as the window is wide, and the
    extension measured from it is large even though price is merely
    trending. If the selection rule is ever changed, this test should
    fail loudly so the change is a conscious one, made against the data
    logged in entry_candidates.breakout_candles_ago.
    """
    candles = []
    p = 100.0
    for i in range(60):
        p *= 1.0025
        candles.append(Candle(timestamp=i, open=p / 1.0025, high=p * 1.002, low=p / 1.0025 * 0.998, close=p, volume=100))
    confirmed, level, idx = detect_breakout(candles)
    assert confirmed is True
    assert idx == len(candles) - 1 - BREAKOUT_SEARCH_WINDOW  # the oldest candle the window allows
    tech = analyze_technicals(candles)
    assert tech.breakout_candles_ago == BREAKOUT_SEARCH_WINDOW
    assert tech.extension_atr_multiples > 2.0  # lands in the -40 penalty band
