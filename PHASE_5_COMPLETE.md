# Phase 5 — Staged Rollout (Complete)

**Deliverable:** [`STAGED_ROLLOUT_PROPOSAL.md`](STAGED_ROLLOUT_PROPOSAL.md)

Phase 5 is **documentation only**. No code changes were made to enable mainnet
execution. The bot still defaults to `EXECUTION_VENUE=TESTNET`.

## What the proposal contains

1. **Hard blockers** — OOS backtest (-17.97% mean net PnL), model retrain required
2. **Stage 0** — Current testnet baseline (7-day soak checklist)
3. **Stage 1** — Model validation gate (retrain + walk-forward must pass)
4. **Stage 2** — 14-day testnet soak with Telegram alerts
5. **Stage 3** — Minimal live pilot ($500 cap, 2x leverage, tighter risk limits)
6. **Stage 4** — Optional scale-up table (manual sign-off each step)
7. **Operator runbook** — Kill switch, rollback, commands
8. **Sign-off sheet** — Required before Stage 3 live capital

## Immediate next steps (operator)

```bash
# 1. Retrain after Phase 0 label alignment
python model_brain.py
python backtest_runner.py

# 2. Review backtest_report.md — must show positive OOS after costs

# 3. Continue testnet soak (Stage 0/2)
streamlit run dashboard.py
```

## Audit complete

| Phase | Status |
|-------|--------|
| 0 — Label/store/reporter fixes | ✅ |
| 1 — Walk-forward backtest | ✅ (strategy unprofitable OOS — flagged) |
| 2 — Risk engine | ✅ |
| 3 — Live-readiness | ✅ |
| 4 — Tests + dashboard honesty | ✅ (82 tests) |
| 5 — Staged rollout proposal | ✅ |

**Do not set `EXECUTION_VENUE=LIVE` until Stage 1 quantitative gates pass.**
