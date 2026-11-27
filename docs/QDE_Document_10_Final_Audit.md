# QDE V1 – Quant Decision Engine
## Technical Design Document
### Document 10 – Final Audit (V1 Codebase Review)
**Version:** 1.0
**Scope:** All 33 delivered files. No new code written. Findings below are cited against actual file/line locations, verified by re-reading the code and, where noted, by re-running it.

---

## 1. Method

Every claim below was checked against the actual code in this session (grep/read), not reconstructed from memory of what was intended. Where a finding could be empirically confirmed (e.g. "is `batch_context` ever non-empty in production?"), I ran it rather than inferred it. I flag severity as **Critical / High / Medium / Low**.

---

## 2. Critical Findings (fix before any real usage)

### 2.1 Uniqueness is non-functional in the real pipeline
`main.py` line 238:
```python
result = self.confidence_engine.evaluate(setup, batch_context=[])
```
`batch_context` is **hardcoded to an empty list** in the only place `ConfidenceEngine.evaluate()` is actually called. `BatchUniquenessProvider.calculate()` returns its neutral default (50.0) whenever `context` is empty — confirmed in our own integration test output (`Uniqueness ....... 50` for every candidate). **Every uniqueness score QDE has ever produced outside of unit tests is a constant 50.0.** Document 04's entire uniqueness/rarity concept (§13–§14) is currently decorative.

**Root cause is architectural, not a typo.** `_process_one()` runs per-symbol, concurrently, and completes before the full batch is known — there is no point inside it where "the current scan batch" actually exists yet. Uniqueness-vs-batch fundamentally requires a **two-pass pipeline**: (1) fetch + filter + score every symbol, (2) *then* run confidence/uniqueness once across the complete `list[Setup]`. The current single-pass-per-symbol design cannot correctly host this feature no matter what is passed in as `batch_context`.

### 2.2 Market regime is always `"unknown"`
`main.py` line 234:
```python
setup = self.scoring_engine.score(features)
```
`ScoringEngine.score()` accepts a `market_regime` parameter defaulting to `"unknown"` — and it is **never passed** anywhere in the real pipeline. This silently disables:
- Document 04 §12 (Market Regime Adjustment — confidence should shift ±5–15% by regime)
- Document 05 §12 (all Research Engine statistics segmented by regime)
- The terminal report's "Market Regime" header, which currently just prints whatever the first candidate's `setup.market_regime` happens to be — i.e. `"unknown"`, always.

This and 2.1 are the two most damaging findings: both features exist, are tested, and *look* wired — but the wiring stops one call short of `main.py`. This is exactly the kind of gap that "28/28 tests passing" does not catch, because the tests exercise the classes correctly in isolation while the orchestrator itself never calls them the way the tests do.

### 2.3 Resource leaks — nothing is ever closed
`main.py` has no `try/finally`, and neither `ExchangeClient.close()` nor `SQLiteRepository.close()` is called anywhere in `main_async()`. Grep confirms zero `.close()` calls in `main.py`. Every run leaks:
- The underlying `aiohttp` session inside ccxt's `async_support` exchange (open sockets/file descriptors).
- The SQLite connection (usually reclaimed on process exit by the OS, but not on repeated in-process runs, e.g. a future scheduler/daemon mode).

### 2.4 Symbol format never normalized
`ExchangeClient.get_active_symbols()` returns ccxt's unified symbol format (`"ETH/USDT:USDT"`), and this string flows unchanged into `Setup.symbol`, the SQLite `setups.symbol` column, JSON/CSV exports, and the terminal report. **Every one of the 8 design documents shows plain Binance symbols (`"ETHUSDT"`, `"ETHFIUSDT"`)** — this is a real, visible inconsistency between spec and implementation, not a cosmetic detail, since `symbol` is a join key used by `get_setups_by_fingerprint` and any future cross-referencing.

---

## 3. Interface / Import / Dependency-Cycle Review

- **No circular imports found.** The import graph established in Document 09 §4 holds exactly as designed; `IndicatorRegistry.discover()`'s lazy `import indicators` inside the function body (not at module load time) is the one place a cycle *could* have appeared, and it was correctly avoided.
- **`DataCollector.fetch_batch()` is dead code.** `main.py` never calls it — the real pipeline calls `self.collector.fetch(symbol)` directly inside its own `asyncio.Semaphore`-bounded task. `fetch_batch()` is only exercised by our tests. Dead code that duplicates error-isolation logic already present elsewhere is a maintenance hazard: if `fetch()`'s failure handling changes, `fetch_batch()` can silently drift out of sync since nothing in production would notice.
- **`HardFilterEngine.required_candles` silent fallback.** When `filters.json`'s `required_candles` is empty/missing, the engine falls back to `{tf: 1 for tf in market_data.timeframes}` — requiring only **1** candle per timeframe. This is a silent-degradation footgun: a config typo doesn't fail loudly, it just makes the Hard Filter much weaker.
- **`get_mark_price()` has an implicit ordering dependency.** It calls `self._exchange.market(symbol)`, which requires `load_markets()` (called inside `get_active_symbols()`) to have already run. This works in the current call order but is not type-enforced or defensively checked — calling `get_mark_price()` standalone (e.g. from a future REST API or notebook) would raise a ccxt `BadSymbol`/`ExchangeError` with a confusing message.

---

## 4. Asyncio & Concurrency Issues

- **`RateLimiter.acquire()` holds its lock across the sleep.** The single `asyncio.Lock` in `utils/rate_limiter.py` is acquired for the entire body of `acquire()`, including the `await asyncio.sleep(wait_time)` call. This means when tokens are exhausted, **every other coroutine waiting to acquire is blocked from even checking the token count**, not just from consuming a token. The rate is still respected, but dispatch becomes convoy-like rather than truly concurrent — a token-bucket implementation using `asyncio.Condition` or a lock-free compare-and-set pattern would let waiters check independently instead of queuing behind whichever coroutine got there first.
- **Blocking SQLite calls inside async code.** `SQLiteRepository` uses the stdlib synchronous `sqlite3` module directly. `ResearchEngine.record()` is called from inside `_process_one()`, an async coroutine running concurrently with up to `max_parallel_symbols` (20) others — every `self._conn.execute(...); self._conn.commit()` call **blocks the entire event loop** for its duration, stalling all other in-flight symbol coroutines. At 500+ symbols this compounds into a real throughput problem and partially defeats the point of the asyncio architecture. Needs `asyncio.to_thread(...)` or a dedicated executor, or an async SQLite driver (`aiosqlite`).
- **Blocking file I/O in the logger.** `storage/logger.py`'s `FileHandler` performs synchronous disk writes on every `.info()/.debug()` call from async code — same class of issue as above, smaller impact but present on every log line, not just DB writes.
- **`async_retry`'s exception net is too wide.** `utils/retry.py` defaults to `exceptions=(Exception,)`. This means a genuine programming bug (`TypeError`, `KeyError`, `AttributeError` from our own code) gets silently retried up to `max_retries` times with exponential backoff *as if it were a transient network failure*, before finally surfacing as a generic `RetryExhaustedError` that obscures the real cause. It should be scoped to ccxt's actual transient-error hierarchy (`ccxt.NetworkError`, `ccxt.RateLimitExceeded`, `ccxt.ExchangeNotAvailable`, `ccxt.RequestTimeout`), not `Exception` broadly.

---

## 5. Quant Logic Critique

### 5.1 Overfitting risk — very real, currently maximal
Every threshold in the scoring/confidence layer (`RSI 45–65 "healthy"`, volatility percentile cutoffs at 40/80, funding thresholds at 0.0001/0.0005, risk-grade cutoffs at 90/80/65/50, indicator weights in `config/weights.json`) is a **hand-picked constant with zero backtest evidence behind it.** Document 08 §6 defines a mandatory validation workflow — Research → Prototype → Historical Backtest → Walk-Forward → Paper → Production — and none of these thresholds have been through it. They went straight from design intuition to "production" code. This is not a bug, but it means the system's current scores should be treated as **illustrative, not predictive**, until Document 08's own validation process is actually run against them.

### 5.2 Scoring range compression
`ScoringEngine`'s additive `_BASELINE = 50` plus per-feature caps (trend ±20, momentum ±15, volume ±18, relative_strength ±20, etc.), combined with the configured weights, makes it very easy to hit the [0,100] clamp ceiling. In our own integration tests, every synthetic "healthy" fixture scored 85–98 regardless of how strong the signal was tuned to be — the model has almost no discriminative range left between "good" and "exceptional" once several features agree. This directly undercuts Document 03 §2's stated goal (probabilistic differentiation, not binary pass/fail) — right now the system behaves closer to binary than the design intends.

### 5.3 Confidence is not independent of Score
`ConfidenceEngine.evaluate()`:
```python
confidence = agreement_based_confidence * 0.8 + setup.raw_score * 0.2
```
Document 04 §2's foundational example ("Coin A, Score 92 / Coin B, Score 92 — completely different confidence") assumes confidence is measured *independently* of score, purely from feature agreement. Blending in 20% of `raw_score` means two setups with identical agreement patterns but different scores will *always* get different confidence values — which is a legitimate design choice, but it is **not** the design Document 04 describes, and it weakens the "same score, different confidence" story that document leads with.

### 5.4 Look-ahead / repainting risk
Indicators consume the **last candle in the returned OHLCV series** as "current price" without any check for whether that candle is closed or still forming. On a live scan, the most recent 15m/1h/4h candle from Binance is very likely still open — EMA/RSI/Bollinger values computed against an unclosed candle will shift (repaint) as that candle progresses, meaning the same symbol scanned twice within the same candle period can legitimately produce different scores from "real" market movement mixed with incomplete-candle noise. This isn't wrong, but it's undocumented, and any future backtest that replays *closed* historical candles will not reproduce live-scan behavior unless this is handled consistently.

### 5.5 Survivorship bias (deferred, not yet triggered — but latent)
`get_active_symbols()` only returns *currently* listed, active USDT-M perpetuals. Delisted/expired symbols silently stop being scanned, and their historical setups remain in the database but are never updated with further performance data. This is fine for live scanning, but the moment `ResearchEngine.get_statistics()` is used to justify a strategy choice (Document 05 §11, §15), the underlying setup population is implicitly survivorship-biased — it only reflects coins that were still listed at scan time, not the full historical universe. Worth flagging now so it's designed around before Document 08's backtest framework is built, not after.

### 5.6 Data leakage / integrity — MFE/MAE placeholder
`ResearchEngine.compute_simple_return()` is a **single point-to-point return**, not the max-favorable/max-adverse-excursion Document 05 §7–§8 actually defines (which requires the highest/lowest price *within* the tracking window, not just the price *at* the horizon). This systematically understates true MFE and MAE. It's honestly commented as a V1 placeholder in the code — but it is not labeled as an approximation anywhere in the *outputs* (JSON/CSV/terminal), so a consumer of those reports has no way to know the MFE/MAE fields aren't the real thing yet.

### 5.7 Fingerprint granularity is too coarse
`Setup.fingerprint` stores only `{feature_name: "positive"|"negative"|"neutral"}` — a 3-state flattening of what were originally continuous 0–100 feature values. `get_setups_by_fingerprint()` will therefore group structurally very different setups (a barely-positive trend and an overwhelmingly-positive trend) into the "same" fingerprint. This inflates apparent pattern-repeat counts in Research Engine statistics and weakens the entire "Setup Fingerprint identifies the structure" premise from Document 05 §5.

---

## 6. Binance API Usage Review

### 6.1 Rate limiting does not model Binance's real weight system
`RateLimiter` is a plain requests-per-second token bucket. Binance Futures' actual limit is a **weight-per-minute budget** (1200 weight/min on most endpoints), where different endpoints and parameters cost different weights (e.g. `klines` weight scales with `limit`: ≤100→1, ≤500→2, ≤1000→5, >1000→10). In `data/exchange.py`, only `load_markets` (weight=5.0) and `get_mark_price` (weight=1.0) pass an explicit weight — every OHLCV, ticker, funding, and open-interest call defaults to `weight=1.0` regardless of its true cost. At 500+ symbols × 3–4 timeframes, this **undercounts real API cost substantially** and risks a 418 IP ban under real load, which our own limiter would have no way of predicting.

### 6.2 Massive missed batching opportunity
`get_ticker()`, `get_funding_rate()`, and `get_mark_price()` are all called **once per symbol**. Binance/ccxt expose bulk equivalents (`fetch_tickers()` for all symbols in one call, `fapiPublicGetPremiumIndex()` called *without* a symbol parameter returns mark price for every symbol in one call) that would replace roughly **1,500 individual REST calls per scan with 2–3 bulk calls.** This directly violates Document 07 §14's own stated principle ("if multiple indicators require the same data, download once, reuse everywhere") — we honored it for OHLCV via the cache, but not for ticker/funding/mark-price, where an even bigger win was available for free.

### 6.3 No differentiated handling of 429 vs 418
`async_retry`'s broad exception catch treats a temporary rate-limit warning (429, should back off and retry) identically to an IP ban (418, should stop hitting the endpoint entirely for the ban duration). Retrying into an active ban with exponential backoff that's too short can prolong or escalate it. Binance's `Retry-After` header (when present) is never read or honored.

---

## 7. Performance Bottlenecks

| Area | Issue | Impact |
|---|---|---|
| **CPU** | `VolatilityIndicator._rolling_band_widths` recomputes SMA/variance from scratch over the full closes series on every call (O(n·period) ≈ 5,000 ops/symbol) instead of an incremental/rolling computation | Moderate at n=250candles×500symbols; will matter more if candle history or symbol count grows |
| **RAM** | `MarketCache` only evicts an entry when it's *read* after expiry — entries that are set but never re-queried accumulate for the life of the process | Real leak in any long-running/scheduled (non-restarting) deployment |
| **RAM/CPU** | `SQLiteRepository.get_setups_by_fingerprint()` and `get_statistics()` both run `SELECT * FROM setups` and filter/aggregate in **Python**, loading the entire table into memory every call | Directly contradicts the Repository-pattern's own stated purpose ("...so it can later handle 10 million setups without changing business logic") — the abstraction is safe, but the current SQLite implementation behind it will not scale even before a Postgres/Timescale swap is needed |
| **I/O** | Blocking SQLite + blocking file logging inside the async pipeline (see §4) | Serializes work that should be concurrent |
| **API** | Per-symbol ticker/funding/mark-price calls instead of bulk (see §6.2) | Largest single fixable performance/quota win available |

---

## 8. Security & Error-Handling Gaps

- **No config schema validation.** Document 09 §11 committed to "configuration validation" as a distinct implementation step; it was effectively skipped. `QDEPipeline.__init__` reads `config.general["timeframes"]`, `["concurrency"]["max_parallel_symbols"]`, etc. via direct bracket indexing — a malformed `config.json` raises a raw `KeyError` deep in construction rather than a clear, actionable validation error at startup.
- **No graceful shutdown.** No SIGINT/SIGTERM handling — a killed process mid-scan leaves connections and the DB in an undefined state (compounds with §2.3's leak).
- **No top-level exception boundary.** `main_async()` has no `try/except` around the whole run — an exception in `get_active_symbols()` itself, or in an output renderer, crashes with a raw Python traceback to stderr, bypassing the structured `QDELogger` entirely, so the failure never reaches `logs/qde.log`.
- **No secrets-management pattern yet.** Document 07 §18 requires API keys/secrets never be exposed. Current V1 is public-data-only so there's nothing to leak today, but there is no established `.env`/keyring pattern in place before V1.1 or V2 inevitably need authenticated endpoints (higher rate limits, account-scoped data). Better to establish the pattern now than retrofit it under time pressure later.
- **What's actually done right, for balance:** every SQLite query uses parameterized `?` placeholders — no SQL injection surface anywhere in `sqlite_repository.py`. Every per-symbol failure in the real pipeline is caught and logged rather than propagating (Document 02 §12 honored correctly at the orchestration level, confirmed by our own integration test with a deliberately-failing symbol). The Repository/UniquenessProvider/IndicatorRegistry abstractions are all real, tested, and correctly enforced (`SetupRepository()` is confirmed non-instantiable; `SQLiteRepository` confirmed to implement every abstract method).

---

## 9. V1.1 / V2 / V3 Development Plan

### V1.1 — Correctness & Hardening (no new features, fixes only)
- Restructure the pipeline into two passes so `batch_context` can be real (§2.1)
- Wire a `market_regime` value into `scoring_engine.score()` — even a simple BTC-trend-based heuristic is better than `"unknown"` everywhere (§2.2)
- Add `try/finally` around `main_async()`; close the exchange session and repository connection (§2.3)
- Normalize symbol format at the `ExchangeClient` boundary (§2.4)
- Move SQLite and file-logging calls off the event loop (§4)
- Narrow `async_retry`'s exception scope to genuinely transient ccxt errors (§4)
- Replace per-symbol ticker/funding/mark-price calls with bulk equivalents (§6.2)
- Implement real per-endpoint weight accounting in `RateLimiter`, and distinguish 429 from 418 (§6.1, §6.3)
- Add config schema validation at startup (§8)
- Fix `get_setups_by_fingerprint`/`get_statistics` to filter in SQL, not Python (§7)
- Add `MarketCache` eviction (max size or periodic sweep) (§7)
- Wire in or delete `DataCollector.fetch_batch()` (§3)
- Label MFE/MAE as approximate in all output surfaces, not just code comments (§5.6)

### V2 — Real Data Depth (matches Document 08's own V2 scope, informed by this audit)
- True OI history tracking (replace the current snapshot-only intensity proxy) — needs a scheduled OI poller and its own repository table
- True BTC-baseline wiring for `relative_strength` (collector attaches `btc_percentage` to every ticker automatically — no fallback path needed)
- A real market-regime detector (volatility + trend-based classifier), replacing the V1.1 heuristic
- A price-polling scheduler enabling true MFE/MAE (highest/lowest over the tracking window, not point-to-point)
- Swap `SQLiteRepository` for `PostgresRepository`/`TimescaleRepository` behind the existing `SetupRepository` interface — this is exactly what the frozen Repository pattern was built for
- Richer fingerprint granularity (quantized magnitude bins, not just positive/negative/neutral)
- Backtest + walk-forward framework (Document 08 §6) to actually validate every weight/threshold currently hand-picked
- Volume Profile, Anchored/Session VWAP, correlation matrix (as originally scoped)

### V3 — Adaptive Intelligence (matches Document 08's V3 scope)
- Feature-importance analysis feeding back into `weights.json` automatically (Document 05 §15's "adaptive weight suggestions," with human approval gate preserved)
- Pattern clustering on the now-richer fingerprints (K-Means/DBSCAN, as Document 05 §14 anticipated)
- Probability calibration for `expected_move` replacing the current score/confidence heuristic with real historical base rates once V2's backtest data exists
- Multi-exchange support behind the existing `ExchangeClient` abstraction boundary
- Real-time WebSocket ingestion replacing polling, feeding the same `MarketData` object so nothing downstream needs to change

---

## 10. Score & Priority List

### Score: **79 / 100**

| Category | Score | Notes |
|---|---|---|
| Architecture & modularity | 9.5/10 | Genuinely clean; frozen decisions (Repository, UniquenessProvider, Registry) held up under real integration testing, not just on paper |
| Test coverage & verification discipline | 9/10 | 28/28 passing, every layer independently and jointly verified — but tests exercise classes correctly in isolation while missing that `main.py` itself doesn't wire two of them correctly (§2.1, §2.2) |
| Functional correctness in production wiring | 5.5/10 | Uniqueness and market regime are non-functional end-to-end despite working in every unit test |
| Quant methodology rigor | 5/10 | Zero backtest validation behind any threshold yet; scoring range compression; confidence/score colinearity |
| Binance API compliance & operational safety | 5/10 | Real weight-limit exposure is understated; large missed batching opportunity; 429/418 not differentiated |
| Performance & scalability | 6/10 | Blocking I/O inside async pipeline; unbounded cache; full-table-scan repository methods |
| Security & error handling | 6.5/10 | No resource leaks in isolated tests, but real ones in the actual entrypoint; no config validation; correctly parameterized SQL throughout |

**Overall read:** this is a well-engineered V1 skeleton — the architecture, testing discipline, and frozen design decisions are genuinely strong and mostly held. What's missing is the last mile of wiring two headline features into the real entrypoint, and the operational hardening (resource cleanup, real Binance weight compliance, non-blocking persistence) that separates "passes its own test suite" from "safe to run unattended against live Binance."

---

## Top 20 Priority Items (ordered)

1. Fix `batch_context=[]` — restructure to a two-pass pipeline so Uniqueness is real (§2.1)
2. Wire real `market_regime` into `scoring_engine.score()` (§2.2)
3. Close `ExchangeClient`/`SQLiteRepository` in `main_async()` via `try/finally` (§2.3)
4. Normalize Binance symbol format at the `ExchangeClient` boundary (§2.4)
5. Move blocking SQLite calls off the event loop (`asyncio.to_thread`/`aiosqlite`) (§4)
6. Replace per-symbol ticker/funding/mark-price calls with bulk equivalents (§6.2)
7. Implement real per-endpoint Binance weight accounting in `RateLimiter` (§6.1)
8. Differentiate 429 (backoff/retry) from 418 (stop, respect ban) (§6.3)
9. Narrow `async_retry`'s exception scope away from blanket `Exception` (§4)
10. Add startup config schema validation with clear failure messages (§8)
11. Fix `get_setups_by_fingerprint`/`get_statistics` to filter in SQL, not Python (§7)
12. Add `MarketCache` eviction/size cap (§7)
13. Wire in or remove dead `DataCollector.fetch_batch()` (§3)
14. Add a top-level exception boundary + graceful shutdown handling in `main.py` (§8)
15. Recalibrate scoring baseline/caps so scores don't cluster at 85–98 (§5.2)
16. Reduce Confidence/Score colinearity to match Document 04's own stated premise (§5.3)
17. Label MFE/MAE as approximate everywhere they're surfaced, not just in code (§5.6)
18. Fix `HardFilterEngine`'s silent 1-candle fallback when `required_candles` is misconfigured (§3)
19. Increase fingerprint granularity beyond 3-state positive/negative/neutral (§5.7)
20. Establish a `.env`/secrets pattern ahead of V1.1/V2's authenticated-endpoint needs (§8)

---

## Closing Note

None of these findings require reopening the frozen architectural decisions from Document 09 — the Repository pattern, UniquenessProvider injection, and Indicator Registry all did exactly their job and are not implicated in any of the above. Every issue found is either (a) a wiring gap between correctly-built components and `main.py`, (b) an operational-safety gap around the live Binance API, or (c) an honest quant-methodology debt that Document 08 §6 already anticipated needing before production trust is warranted. The architecture held; the last-mile wiring and operational hardening did not, yet.
