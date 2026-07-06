# BTC/USDT ML Futures Bot — Version History

This document tracks **every major release** of the bot: what each version does, how
to identify it at runtime, and what changed from the previous version.

**Current release:** `v2.0.4` · **Default profile:** `COMPOUND` (Path B)  
**Legacy profile still available:** `SWING` (v1.x behaviour via `TRADING_PROFILE=SWING`)

---

## Quick reference


| Version    | Codename / profile  | Timeframe | Default? | One-line summary                               |
| ---------- | ------------------- | --------- | -------- | ---------------------------------------------- |
| **v0.9.0** | Baseline            | 1h        | —        | CSV log, basic 1h swing bot                    |
| **v1.0.0** | Foundation          | 1h        | —        | SQLite store, aligned labels, session reporter |
| **v1.1.0** | Rigor               | 1h        | —        | Walk-forward backtest with costs               |
| **v1.2.0** | Guardrails          | 1h        | —        | Risk engine wired into live loop               |
| **v1.3.0** | Hardening           | 1h        | —        | Retry, alerts, TESTNET/LIVE gate               |
| **v1.4.0** | Honesty             | 1h        | —        | 84+ tests, dashboard truth fixes               |
| **v1.5.0** | Rollout plan        | 1h        | —        | Staged mainnet proposal (docs only)            |
| **v1.6.0** | Desk UI             | 1h        | —        | Retro trade log, heartbeat strip               |
| **v1.6.1** | Stability           | 1h        | —        | Deadlock fix, boot heartbeat                   |
| **v2.0.0** | **COMPOUND**        | **15m**   | **✅**    | Active compounding strategy (Path B)           |
| **v2.0.1** | COMPOUND + export   | 15m       | —        | Session CSV dossier on shutdown                |
| **v2.0.2** | COMPOUND + audit    | 15m       | —        | Honest threshold optimizer + label sweep tool  |
| **v2.0.3** | COMPOUND + features | 15m       | —        | Phase 3 feature groups + aligned scalp brackets |
| **v2.0.4** | COMPOUND + F2 tune  | 15m       | **✅**    | Asymmetric thresholds, F2 audit, capital-preserving OOS |


---

## How to see which version you are running


| Signal              | Where to look                                            |
| ------------------- | -------------------------------------------------------- |
| **Profile**         | Dashboard banner: `… BTCUSDT 15m 3x                      |
| **Timeframe**       | Same banner (`15m` = Compound, `1h` = Swing)             |
| **Thresholds**      | `decision_threshold.json` or dashboard expander          |
| **Exports**         | `session_exports/` folder exists → v2.0.1+               |
| **Label sweep**     | `label_sweep.py` / `label_sweep_report.md` → v2.0.2+     |
| **Feature sweep**   | `feature_sweep.py` / `feature_sweep_report.md` → v2.0.3+ |
| **Feature variant** | `.env` `FEATURE_VARIANT=F2` (recommended) → v2.0.4+      |
| **Prediction audit** | `audit_predictions.py` → v2.0.4+                        |
| **Retrain CLI**     | `python model_brain.py --retrain` → v2.0.4+            |
| **Compound strip**  | 7d PnL / expectancy row on dashboard → v2.0.0+           |


Switch profile in `.env`:

```bash
TRADING_PROFILE=COMPOUND   # v2.x (default)
TRADING_PROFILE=SWING      # v1.x legacy — retrain on 1h data after switching
```

---

## Version details

### v0.9.0 — Baseline (pre-foundation)

**Characteristics**

- Binance USDⓈ-M **Futures Testnet** execution, mainnet klines for data
- **1-hour** candles, 3-class XGBoost (LONG / SHORT / CASH)
- Persistence: append-only **CSV** (`bot_status_log.csv`)
- TP/SL: +1.5% / −1.0% (later aligned in v1.0.0)
- Single-position bracket manager, dashboard with gauges and terminal feed
- No walk-forward gate, no risk engine, no instance lock

**Typical pain points that motivated v1.0.0**

- CSV corruption under concurrent dashboard + bot writes
- Training labels and live TP/SL could drift apart
- No ground-truth trades table for session reports

---

### v1.0.0 — Foundation (Phase 0)

**Characteristics**


| Area        | Value                                                                 |
| ----------- | --------------------------------------------------------------------- |
| Timeframe   | 1h                                                                    |
| TP / SL     | +1.2% / −0.6% (single source of truth in `config.py`)                 |
| Persistence | **SQLite WAL** (`bot_status_log.db`) — `status_log` + `trades` tables |
| Thresholds  | Tuned sidecar `decision_threshold.json` (e.g. 0.45 / 0.775)           |
| Allocation  | 25% wallet margin × leverage                                          |
| Loop        | 7s                                                                    |
| Session end | `session_summary_report.md` + PDF from dashboard                      |


**Key modules added/changed:** `trade_store.py`, threshold sidecar, session reporter in `bot_loop.py`, instance lock (`.bot_instance.lock`).

**Changed from v0.9.0 → v1.0.0**

- CSV → SQLite atomic writes
- Label factory and live bot share identical TP/SL constants
- Completed trades written atomically at close (no log parsing)
- Data-driven thresholds saved at train time

---

### v1.1.0 — Rigor (Phase 1)

**Characteristics**

- **Walk-forward backtest** (`backtest_runner.py`) with taker fees + slippage
- Cost-aware equity curve, Sharpe/Sortino, per-fold threshold audit
- Report: `backtest_report.md`
- Finding documented: strategy **unprofitable OOS** (~−18% mean net PnL on 1h) — live gate flagged

**Changed from v1.0.0 → v1.1.0**

- Added rigorous OOS evaluation; no change to live trading logic yet
- Exposed that high selective thresholds ≠ profitable edge

---

### v1.2.0 — Guardrails (Phase 2)

**Characteristics**


| Risk control           | Default                               |
| ---------------------- | ------------------------------------- |
| Session loss halt      | −3% of session-start equity           |
| Consecutive loss pause | 4 losses → manual resume              |
| Vol-scaled sizing      | ATR shrinks margin in high vol        |
| Kill switch            | File + in-memory (`.bot_kill_switch`) |
| Order sanity           | Price deviation guard                 |


**Key module:** `risk_engine.py` — all new entries pass through `check_can_open()` / `check_session_loss_limit()`.

**Changed from v1.1.0 → v1.2.0**

- Model signal → order path now has hard limits
- Dashboard sidebar: risk resume, kill switch controls

---

### v1.3.0 — Hardening (Phase 3)

**Characteristics**

- `**EXECUTION_VENUE`**: `TESTNET` (default) or `LIVE` with dashboard `LIVE` confirmation
- `**exchange_client.py`**: shared retry/backoff, reconnect after repeated failures
- `**alerting.py`**: Telegram optional; rate-limited alerts on errors, opens, closes
- Connection **DEGRADED** state: bracket exits continue; new entries paused
- `.env.example` / `.gitignore` hygiene; `LIVE_READINESS_CHECKLIST.md`

**Changed from v1.2.0 → v1.3.0**

- All API calls route through `call_with_retry()`
- Operator cannot accidentally boot mainnet without typing `LIVE`
- Degraded API no longer silently treated as flat

---

### v1.4.0 — Honesty (Phase 4)

**Characteristics**

- **84 automated tests** (`pytest tests/ -q`) covering labeling, state machine, risk, venue, dashboard stats
- `**dashboard_stats.py`**: pure helpers; floating PnL reconciled with exchange when possible
- Dashboard no longer implies profit from stale log rows
- Threshold panel uses same `resolve_live_thresholds()` as the bot

**Changed from v1.3.0 → v1.4.0**

- Test suite and dashboard metric honesty
- `PHASE_4_CHECKLIST.md` exit criteria

---

### v1.5.0 — Rollout plan (Phase 5)

**Characteristics**

- **Documentation only** — no live-trading code changes
- `STAGED_ROLLOUT_PROPOSAL.md`: Stage 0 testnet soak → Stage 3 live pilot ($500 cap, 2x leverage)
- Explicit blockers: OOS backtest must pass before LIVE

**Changed from v1.4.0 → v1.5.0**

- Operator runbook and sign-off sheet; default remains TESTNET

---

### v1.6.0 — Desk UI (dashboard redesign)

**Characteristics**

- **Retro terminal trade log** (green wins / red losses) from SQLite `trades` table
- **Essential metrics strip**: Wallet, Net PnL, Open PnL, Win Rate
- **Engine heartbeat**: LIVE / BOOTING / STALE / DEGRADED / OFFLINE
- Entry/exit prices and hold minutes (SH) in trade log

**Changed from v1.5.0 → v1.6.0**

- UI focused on operator clarity; fewer misleading legacy gauges

---

### v1.6.1 — Stability (critical bugfix release)

**Characteristics**

- Same strategy as v1.6.0 / v1.x **SWING** profile
- **Deadlock fix**: `_get_usdt_balance()` no longer called under `state._lock` (bot was freezing with 0 log rows)
- **Boot heartbeat**: status row written immediately when loop starts
- **BOOTING** vs **STALE** distinction when log is empty but engine just started
- Iteration errors and empty-candle paths still write status rows

**Changed from v1.6.0 → v1.6.1**

- Dashboard shows LIVE + wallet within seconds of boot
- No functional strategy change — reliability only

**This is the last 1h “selective swing” release.** Preserved as `TRADING_PROFILE=SWING`.

---

### v2.0.0 — COMPOUND (Path B) ★ current strategy

**Codename:** Active Compounding  
**Default:** `TRADING_PROFILE=COMPOUND`

**Characteristics**


| Area                  | COMPOUND (v2.0.0)                                              | SWING (v1.6.1)                |
| --------------------- | -------------------------------------------------------------- | ----------------------------- |
| **Timeframe**         | **15m**                                                        | 1h                            |
| **Scan loop**         | **5s**                                                         | 7s                            |
| **TP / SL (base)**    | **+0.4% / −0.25%**                                             | +1.2% / −0.6%                 |
| **Brackets**          | ATR-scaled optional                                            | Fixed % only                  |
| **Trailing stop**     | **On** (+0.4% act, 0.2% trail)                                 | Off                           |
| **Allocation**        | **12%** margin                                                 | 25% margin                    |
| **Thresholds**        | Data-driven per side (e.g. **0.49 / 0.60** F2)               | High (e.g. 0.45/0.775)        |
| **Trade frequency**   | High (many scans/day)                                          | Low (often hours idle)        |
| **Re-entry cooldown** | **60s** after TP/SL                                            | None                          |
| **Loss halt**         | **−5%** session budget                                         | −3%                           |
| **Sizing extras**     | Win/loss **streak multiplier** (0.5×–1.25×)                    | Vol-scale only                |
| **Training data**     | `historical_btc_15m.parquet` (2y)                              | `historical_btc.parquet` (4y) |
| **Label horizon**     | 16 bars (~4h forward)                                          | 24 bars (~24h forward)        |
| **Dashboard**         | **Compound strip** (7d PnL, expectancy, size mult, signal gap) | Standard metrics only         |


**Key modules added**

- `compound_strategy.py` — brackets, trailing stop, streak sizing, threshold distance
- `TRADING_PROFILE` switch in `config.py`
- Threshold optimizer requires **positive validation PnL** (see v2.0.4 for current min-trade and fallback rules)

**Mindset**

- Many small wins reinvested via **wallet-based sizing**
- Success = **positive weekly expectancy**, not a fixed daily % target
- OOS backtest on 15m retrain still **negative (~−22%)** — testnet validation only

**Changed from v1.6.1 → v2.0.0**

- Major strategy rewrite; old 1h model **must not** be used with COMPOUND profile
- Retrain required: `python model_brain.py` after profile switch
- See `PATH_B_COMPOUND.md` for env knobs

---

### v2.0.1 — Session CSV export

**Characteristics**

- Everything in **v2.0.0 COMPOUND**
- On shutdown (FORCE SHUTDOWN, Ctrl+C, or `bot.stop()`):
  - `session_summary_report.md` (unchanged)
  - **New:** `session_exports/session_{start}_{duration}.csv`
- CSV sections: **metrics** · **full session status log** · **completed trades**
- Filename example: `session_2026-07-05_18h03m_2h15m30s.csv`
- Dashboard shutdown notice shows PDF + CSV paths
- Hot-reload safe: CSV back-filled if missing on cached reports

**Changed from v2.0.0 → v2.0.1**

- `session_export.py` + `SESSION_EXPORT_DIR` config
- Shutdown no longer crashes if report object lacks `csv_path` (Streamlit cache fix)

---

### v2.0.2 — Threshold honesty + label sweep

**Date:** 2026-07-06

**Why this release**

Overnight testnet session (2026-07-06, ~7h, COMPOUND 15m) produced **zero trades** with
probabilities clustered near a flat 1/3 split (peaks 0.395 long / 0.409 short). Task 1
OOS audit confirmed the model **does not beat a naive class-frequency baseline** and
all five walk-forward folds are **negative** (mean −28% net PnL). The old threshold
optimizer still chose **0.48/0.48** because it picked the *least negative* validation
slice among thresholds with ≥25 trades — not a threshold with real edge.

This release fixes that selection bias and adds tooling to compare label configurations
before committing to a full retrain. **No TP/SL, live thresholds, or trade-frequency
knobs were changed** — only how thresholds are *chosen* at train time and how we audit
labels offline.

**Characteristics**


| Area                       | v2.0.2 change                                                                                           |
| -------------------------- | ------------------------------------------------------------------------------------------------------- |
| **Threshold optimizer**    | Only accepts validation net PnL **> 0** and ≥40 trades (COMPOUND default, was 25)                       |
| **No-edge fallback**       | Direction disabled at `THRESHOLD_DISABLED` (1.01) with `NO_EDGE_THRESHOLD` log — no more 0.48-at-a-loss |
| **Activity bonus removed** | COMPOUND no longer rewards trade count when validation PnL is negative                                  |
| **Label sweep**            | New `label_sweep.py` — compares L1–L4 horizon/TP-SL variants, writes `label_sweep_report.md`            |
| **Sizing (prior patch)**   | Exchange-min floor only when % sizing is below Binance min notional (not fixed $60 bump)                |


**Key files**

- `model_brain.optimize_thresholds()` — positive-PnL gate + disable sentinel
- `config.THRESHOLD_DISABLED` — `1.01` shared constant
- `config.MIN_VALIDATION_TRADES` — COMPOUND default **40** (env override still supported)
- `label_sweep.py` — Phase 1 retrain experiments (`python label_sweep.py` or `--quick`)
- `tests/test_optimize_thresholds.py` — regression tests for optimizer rules

**Operator notes**

- Existing `decision_threshold.json` (0.48/0.48) is **unchanged on disk** until you retrain:
`python model_brain.py`
- After retrain with v2.0.2 optimizer, expect **DISABLED** thresholds (1.01) if validation
has no profitable direction — that is intentional (flat is better than forced losses).
- Run label comparison: `python label_sweep.py` (full) or `python label_sweep.py --quick`

**Changed from v2.0.1 → v2.0.2**

- Threshold optimizer requires positive validation PnL; removes COMPOUND activity bonus
- `THRESHOLD_DISABLED` constant; `MIN_VALIDATION_TRADES` default 40 for COMPOUND
- New `label_sweep.py` + `label_sweep_report.md` output
- `feature_factory.build_target()` — resolves config TP/SL at call time (label sweep fix)

---

### v2.0.3 — Phase 3 feature experiments

**Date:** 2026-07-06

**Why this release**

Phase 1 label sweep (L1–L4) showed **no variant beats the log-loss baseline** — label
horizon/TP-SL tweaks alone cannot fix the flat ~0.33 live probabilities. Phase 3 adds
new **input features** and an offline sweep to find whether regime, multi-timeframe,
time-of-day, or flow proxies improve calibration before retraining.

**Feature variants**


| ID     | Adds                                                               |
| ------ | ------------------------------------------------------------------ |
| **F0** | Baseline 19 TA features (production default)                       |
| **F1** | `bb_width_pct`, `atr_pct_z` — vol regime                           |
| **F2** | `rsi_1h`, `macd_hist_1h`, `ema_spread_1h` — 1h context on 15m rows |
| **F3** | `hour_sin/cos`, `dow_sin/cos` — session cyclical encoding          |
| **F4** | `volume_z`, `close_in_bar` — flow / microstructure proxies         |
| **F5** | All Phase 3 groups combined (30 features)                          |


**Key files**

- `feature_factory.py` — Phase 3 groups, `feature_columns_for()`, `use_feature_variant()`
- `feature_sweep.py` — compares F0–F5 (`python feature_sweep.py` or `--quick`)
- `config.FEATURE_VARIANT` — default `F0`; set to winner before `python model_brain.py`
- `tests/test_phase3_features.py`

**Deploy gate (feature sweep)**

Promote a variant only if it **beats log-loss baseline** and walk-forward mean PnL
**> −5%** with **≥2/5 positive folds**. Otherwise stay on F0 or proceed to Phase 4.

**Changed from v2.0.2 → v2.0.3**

- Phase 3 feature groups in `feature_factory.py`
- `feature_sweep.py` + `feature_sweep_report.md`
- `FEATURE_VARIANT` env knob wired into train + live inference
- **COMPOUND scalp brackets aligned:** `TAKE_PROFIT_PCT=0.004`, `STOP_LOSS_PCT=0.0025`, `FORWARD_WINDOW=16` — labels, backtest, and live execution share `config.py`

---

### v2.0.4 — F2 tuning + asymmetric thresholds (current)

**Date:** 2026-07-06

**Why this release**

After aligning labels to **+0.4% / −0.25%** scalp brackets and promoting the **F2**
(1h multi-timeframe) feature variant, validation showed strong directional PnL on
tight thresholds — but walk-forward and live behavior exposed three problems:

1. **0.55 uniform fallback** caused hyper-trading (700+ trades/fold) and fee drag (−55% Fold 1).
2. **0.78 uniform fallback** preserved capital (−1.81% OOS vs −11.3% B&H) but **locked out shorts** in downtrends.
3. Optimizer needed a finer grid and lower min-trade floor for 15m scalp distributions.

**Threshold optimizer (COMPOUND defaults)**


| Setting | Value | Purpose |
| ------- | ----- | ------- |
| `MIN_VALIDATION_TRADES` | **5** | Enough signal for sparse 15m folds without forcing fallback |
| `THRESHOLD_SEARCH_MIN/MAX/STEP` | **0.34 – 0.85 / 0.01** | Fine organic search up to high conviction |
| `LONG_FALLBACK_THRESHOLD` | **0.78** | Conservative long entries when no organic pass |
| `SHORT_FALLBACK_THRESHOLD` | **0.60** | More aggressive short capture in flushes (not 0.78) |
| Positive-PnL gate | **still on** | Organic thresholds must beat 0% net on validation slice |

**Current trained artifact (F2, 2026-07-06)**


| Metric | Value |
| ------ | ----- |
| `FEATURE_VARIANT` | **F2** (22 features) |
| `decision_threshold.json` | **long 0.49** (organic) · **short 0.60** (fallback) |
| OOS strategy PnL (20% holdout) | **−1.81%** |
| Buy & hold (same period) | **−11.30%** |
| OOS trades | 17 long / 0 short (holdout slice; live shorts enabled at 0.60) |

**New / updated tooling**

- `audit_predictions.py` — F2 holdout audit vs scalp ground-truth labels; top-10 high-confidence examples
- `xgboost_trading_model_f2.json` — optional F2 artifact path used by audit script
- `python model_brain.py --retrain` — explicit retrain + threshold sidecar overwrite

**Walk-forward stabilization (feature sweep, post-0.78-long fallback)**

After replacing 0.55 with 0.78 long fallback, walk-forward mean PnL stabilized to
**≈ −2.15%** across F0–F5 (vs catastrophic −55% single-fold bleed). F2/F5 pick organic
thresholds (~0.51–0.60) on validation when profitable.

**Key files**

- `config.py` — `LONG_FALLBACK_THRESHOLD`, `SHORT_FALLBACK_THRESHOLD`
- `model_brain.optimize_thresholds()` — per-side fallbacks
- `audit_predictions.py` + `tests/test_audit_predictions.py`

**Operator notes**

```bash
# .env (recommended)
TRADING_PROFILE=COMPOUND
FEATURE_VARIANT=F2

python model_brain.py --retrain    # refresh model + decision_threshold.json
python audit_predictions.py      # spot-check F2 vs scalp labels
python feature_sweep.py          # walk-forward gate check
python backtest_runner.py
```

**Changed from v2.0.3 → v2.0.4**

- Asymmetric fallbacks: long **0.78**, short **0.60**
- Min validation trades **5**; threshold grid **0.01** steps to **0.85**
- Removed destructive **0.55** single fallback (replaced by per-side logic)
- `audit_predictions.py` for F2 historical verification
- `model_brain.py --retrain` CLI flag
- Production retrain on F2 + aligned scalp brackets; capital-preserving OOS (−1.81% vs −11.3% market)

---

## Side-by-side: SWING vs COMPOUND (today)

```
                    SWING (v1.6.1)              COMPOUND (v2.0.4)
                    ─────────────────           ────────────────────
Profile env         TRADING_PROFILE=SWING       TRADING_PROFILE=COMPOUND (default)
Candles             1h                          15m
Typical trades/day  Very few                    Several possible
Goal                Selective quality setups    Active reinvestment / small wins
Compounding         Wallet-sized entries only   + streak size multiplier
Best for            Observing rare signals      Testnet frequency / learning
Retrain data        historical_btc.parquet      historical_btc_15m.parquet
```

---

## Migration guide

### Use COMPOUND (default — already active if you never set SWING)

```bash
# .env (optional — COMPOUND is the code default)
TRADING_PROFILE=COMPOUND

# One-time or after config / feature / bracket change
python model_brain.py --retrain
streamlit run dashboard.py
# → Boot from sidebar
```

### Revert to legacy SWING (v1.6.1 behaviour)

```bash
# .env
TRADING_PROFILE=SWING

# Must retrain on 1h data — do not reuse the 15m model
python model_brain.py
streamlit run dashboard.py
```

---

## Changelog summary (what changed between versions)


| From → To       | Main change                                                                    |
| --------------- | ------------------------------------------------------------------------------ |
| v0.9 → v1.0     | SQLite, aligned TP/SL labels, trades ledger, threshold sidecar                 |
| v1.0 → v1.1     | Walk-forward backtest + cost model                                             |
| v1.1 → v1.2     | Risk engine (loss limits, kill switch, vol sizing)                             |
| v1.2 → v1.3     | Retry/reconnect, Telegram alerts, LIVE gate                                    |
| v1.3 → v1.4     | 84+ tests, dashboard honesty refactor                                          |
| v1.4 → v1.5     | Staged rollout documentation (no code)                                         |
| v1.5 → v1.6.0   | Dashboard redesign (trade log, heartbeat, essential metrics)                   |
| v1.6.0 → v1.6.1 | **Bugfix:** balance-fetch deadlock, boot heartbeat, STALE UX                   |
| v1.6.1 → v2.0.0 | **Strategy:** Path B COMPOUND (15m, trailing, streak sizing, lower thresholds) |
| v2.0.0 → v2.0.1 | **Feature:** session CSV export on shutdown                                    |
| v2.0.1 → v2.0.2 | **Audit:** honest threshold optimizer, label sweep tool, sizing floor fix      |
| v2.0.2 → v2.0.3 | **Features:** Phase 3 groups (F0–F5) + aligned scalp TP/SL (0.4%/0.25%)     |
| v2.0.3 → v2.0.4 | **Tune:** asymmetric thresholds (L0.78/S0.60), F2 audit, `--retrain` CLI   |


---

## Related docs


| File                          | Contents                                                               |
| ----------------------------- | ---------------------------------------------------------------------- |
| `PATH_B_COMPOUND.md`          | Compound profile env vars and setup                                    |
| `deploy/ORACLE_DEPLOY.md`     | **Oracle Always Free VM deployment (24/7 + dashboard via SSH tunnel)** |
| `STAGED_ROLLOUT_PROPOSAL.md`  | Mainnet rollout stages (v1.5.0)                                        |
| `PHASE_4_CHECKLIST.md`        | Test and dashboard honesty criteria                                    |
| `LIVE_READINESS_CHECKLIST.md` | Phase 3 hardening evidence                                             |
| `backtest_report.md`          | Latest walk-forward numbers (regenerate with `backtest_runner.py`)     |
| `label_sweep_report.md`       | Label horizon / TP-SL comparison (`python label_sweep.py`)             |
| `feature_sweep_report.md`     | Phase 3 feature comparison (`python feature_sweep.py`)               |
| `audit_predictions.py`        | F2 holdout prediction audit vs scalp ground truth                    |


---

*Last updated: 2026-07-06 · Current code release: **v2.0.4** · Default profile: **COMPOUND** · Recommended: `FEATURE_VARIANT=F2`*