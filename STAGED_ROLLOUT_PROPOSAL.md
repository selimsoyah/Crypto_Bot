# Phase 5 — Staged Rollout Proposal

**Status:** PROPOSAL ONLY — no live execution has been enabled.  
**Default remains:** `EXECUTION_VENUE=TESTNET`  
**Generated:** 2026-07-05  

This document is the final deliverable of the paid due-diligence hardening audit
(Phases 0–5). It defines **how** to move from testnet validation to mainnet
capital **only after explicit operator sign-off at each gate**.

---

## Executive summary

| Item | Current state | Required before Stage 3 (live capital) |
|------|---------------|----------------------------------------|
| Engineering hardening | Phases 0–4 complete | ✅ Done |
| Test suite | 82 tests passing | ✅ Done |
| Risk engine | Kill switch, session loss, consecutive-loss pause | ✅ Wired |
| OOS profitability | **Mean net PnL -17.97%** after costs (5 folds) | ❌ **BLOCKER** |
| Model retrain post Phase 0 label fix | Not verified | ❌ **BLOCKER** |
| Testnet soak (≥7 days) | Not documented | ⏳ Operator task |

**Recommendation:** Do **not** set `EXECUTION_VENUE=LIVE` until the model shows
positive walk-forward net PnL after costs **and** a clean testnet soak period.

---

## Hard blockers (do not skip)

1. **Retrain the model** after Phase 0 TP/SL alignment:
   ```bash
   python model_brain.py
   python backtest_runner.py
   ```
   Review `backtest_report.md`. All five folds must not show systematic losses
   after the 0.140% round-trip cost model.

2. **Rotate API keys** if `.env.example` ever held real credentials (Phase 3 audit).

3. **Confirm Telegram alerts** fire on kill switch and session loss (test on testnet).

4. **Written sign-off** at the bottom of this document before Stage 3.

---

## Architecture reminder (split venue)

| Layer | Source | Config |
|-------|--------|--------|
| Market data (klines) | Mainnet futures public API | `USE_MAINNET_DATA=true` |
| Order execution | Testnet **or** Live | `EXECUTION_VENUE` |

On testnet, you trade with fake USDT against real mainnet prices. That is
intentional for signal validation but **does not prove live fill quality**.
Stage 2 exists to catch execution-specific issues before real capital.

---

## Staged rollout (5 stages)

### Stage 0 — Baseline (current)

**Goal:** Safe default; no mainnet orders.

| Setting | Value |
|---------|-------|
| `EXECUTION_VENUE` | `TESTNET` |
| `LEVERAGE` | `3` (testnet only) |
| Capital | Testnet USDT (free from faucet) |

**Actions:**
- Run `streamlit run dashboard.py` + boot engine on testnet.
- Verify green **TESTNET EXECUTION** banner.
- Exercise kill switch, force shutdown PDF, risk resume after consecutive losses.

**Exit criteria:**
- [ ] ≥7 calendar days continuous testnet operation without uncaught exceptions
- [ ] Session reports reviewed; SQLite ledger matches exchange positions
- [ ] Kill switch tested at least once

---

### Stage 1 — Model validation gate

**Goal:** Prove the strategy is not random after aligned labels and costs.

**Actions:**
```bash
python model_brain.py          # retrain with aligned TP/SL labels
python threshold_sweep.py      # optional threshold audit
python backtest_runner.py      # walk-forward report
pytest tests/ -q               # must stay green
```

**Exit criteria (quantitative):**
- [ ] Walk-forward **mean net PnL ≥ 0%** after costs (currently **-17.97%**)
- [ ] Mean Sharpe > 0 on OOS folds (currently **-7.88**)
- [ ] Minimum 30 OOS trades per fold direction with precision > random (~33%)
- [ ] `decision_threshold.json` sidecar regenerated and matches live resolver

**If this gate fails:** Stop. Do not proceed. Fix features, labels, thresholds,
or abandon live deployment.

---

### Stage 2 — Testnet soak with production settings

**Goal:** Run the hardened bot as if live, still on testnet.

**Recommended `.env` overrides (testnet only):**

```bash
EXECUTION_VENUE=TESTNET
LEVERAGE=3
RISK_MAX_DAILY_LOSS_PCT=0.03
RISK_MAX_CONSECUTIVE_LOSSES=4
TELEGRAM_BOT_TOKEN=<your_bot>
TELEGRAM_CHAT_ID=<your_chat>
```

**Monitor daily:**
- Session loss limit never breached unexpectedly
- Dashboard PnL source shows `EXCHANGE` when API healthy
- No silent-flat position states during API blips
- Alert delivery on open/close/risk halt

**Exit criteria:**
- [ ] ≥14 days testnet soak with retrained model
- [ ] Net testnet PnL positive or explainable (fees/slippage documented)
- [ ] Zero instance-lock collisions (single engine rule respected)
- [ ] Operator runbook reviewed (below)

---

### Stage 3 — Minimal live capital (pilot)

**Goal:** First real mainnet orders with capped risk.

> ⚠️ Requires explicit sign-off. Type `LIVE` in dashboard when booting.

**Pre-flight checklist:**
- [ ] Stage 1 and Stage 2 exit criteria met
- [ ] Separate Binance **mainnet** API key (not testnet key)
- [ ] API key permissions: **Futures only**, withdraw disabled, IP whitelist on
- [ ] Kill switch file absent: `rm -f .bot_kill_switch`
- [ ] Telegram alerts verified on testnet

**Recommended live `.env` (operator applies manually):**

```bash
EXECUTION_VENUE=LIVE
LEVERAGE=2                    # RISK_RECOMMENDED_LIVE_LEVERAGE — lower than testnet 3x
CASH_ALLOCATION_PCT=0.10      # 10% base margin cap for pilot (override via .env if added)
RISK_MAX_DAILY_LOSS_PCT=0.02  # tighter: -2% session halt for pilot week
RISK_MAX_CONSECUTIVE_LOSSES=3
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Note: `CASH_ALLOCATION_PCT=0.10` is a **proposal** — add to `.env` only if you
accept reduced sizing. Default code remains 25% until you change it.

**Capital cap:** Start with **≤ $500 USDT** isolated margin on BTCUSDT only.
Do not scale until Stage 4 criteria met.

**Boot procedure:**
1. Set `.env` as above; restart dashboard/CLI.
2. Confirm red **LIVE MAINNET EXECUTION** banner.
3. Type `LIVE` in sidebar confirmation field.
4. Boot engine; verify Telegram CRITICAL alert for live venue.
5. Watch first trade open/close alerts manually.

**Kill criteria (immediate halt):**
- Session loss limit hit
- Kill switch engaged
- 3 consecutive live losses (pilot `RISK_MAX_CONSECUTIVE_LOSSES=3`)
- Dashboard shows API DEGRADED for >30 minutes with open position
- Any unexplained PnL mismatch between exchange and SQLite ledger

**Rollback:**
```bash
# Dashboard: FORCE SHUTDOWN or KILL SWITCH
# Then in .env:
EXECUTION_VENUE=TESTNET
# Clear kill switch after review:
rm -f .bot_kill_switch
```

**Exit criteria (pilot week):**
- [ ] 7 days live with capped capital
- [ ] Net PnL ≥ -2% (pilot loss budget) or positive
- [ ] No manual intervention required except scheduled reviews
- [ ] All trades in SQLite `trades` table reconcile to Binance history

---

### Stage 4 — Scaled live (optional)

**Goal:** Increase size only after pilot success.

**Prerequisites:** Stage 3 exit criteria + written review of session PDFs.

**Suggested progression:**

| Week | Leverage | Base alloc | Max session loss | Capital cap |
|------|----------|------------|------------------|-------------|
| Pilot (S3) | 2x | 10% | -2% | $500 |
| Week 2 | 2x | 15% | -2.5% | $1,000 |
| Week 4+ | 2x | 20% | -3% | Scale per risk tolerance |

Never exceed `LEVERAGE=3` on live without a separate risk review. The codebase
recommends **2x** for initial live (`RISK_RECOMMENDED_LIVE_LEVERAGE`).

**Never auto-scale.** Each row requires operator sign-off and `.env` update.

---

## Operator runbook (quick reference)

| Event | Action |
|-------|--------|
| Kill switch engaged | Flatten + stop; review logs; clear `.bot_kill_switch` after root cause |
| Consecutive-loss pause | Dashboard → **Confirm Risk Resume** only after reviewing last trades |
| Session loss limit | Bot auto-flattens and stops; do not reboot until cause understood |
| API degraded | No new entries; brackets still managed; check Binance status |
| FORCE SHUTDOWN | Graceful stop + session PDF; verify `session_summary_report.md` |
| Dual engine refused | Stop CLI bot or other dashboard tab; one instance lock only |

**Commands:**
```bash
streamlit run dashboard.py
python bot_loop.py
pytest tests/ -q
python backtest_runner.py
```

---

## Configuration reference (live vs testnet)

| Parameter | Testnet default | Live proposal (Stage 3) |
|-----------|-----------------|-------------------------|
| `EXECUTION_VENUE` | `TESTNET` | `LIVE` |
| `LEVERAGE` | `3` | `2` |
| `CASH_ALLOCATION_PCT` | `0.25` | `0.10` (pilot) |
| `RISK_MAX_DAILY_LOSS_PCT` | `0.03` | `0.02` |
| `RISK_MAX_CONSECUTIVE_LOSSES` | `4` | `3` |
| Dashboard boot confirm | N/A | Type `LIVE` |

All changes are **manual `.env` edits** — the codebase will not silently apply
live parameters.

---

## Audit trail (Phases 0–4 completed)

| Phase | Deliverable |
|-------|-------------|
| 0 | TP/SL alignment, SQLite ledger, threshold sidecar, reporter |
| 1 | Cost-aware walk-forward backtest (`backtest_report.md`) |
| 2 | `risk_engine.py` — circuit breakers, vol sizing, kill switch |
| 3 | Venue banner, retry/reconnect, Telegram alerts, secrets hygiene |
| 4 | 82 tests, `dashboard_stats.py` honesty, coverage tooling |

---

## Sign-off (required for Stage 3+)

By enabling `EXECUTION_VENUE=LIVE`, the operator confirms:

- [ ] I have read this entire proposal
- [ ] Stage 1 backtest shows acceptable OOS performance **after retrain**
- [ ] Stage 2 testnet soak is complete
- [ ] I accept capped pilot capital and kill criteria above
- [ ] I will not scale leverage or allocation without a new written review

**Operator name:** ___________________________  
**Date:** ___________________________  
**Pilot capital cap (USDT):** ___________________________  

---

**Phase 5 exit criteria:** This proposal exists; no live code paths were enabled
without operator action. Hardening audit complete.

**Do not proceed to live trading until Stage 1 blockers are cleared.**
