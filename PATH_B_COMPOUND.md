# Path B — Active Compounding Mode

**Profile:** `TRADING_PROFILE=COMPOUND` (default)

## Mindset

- Many **small, controlled wins** reinvested into the next trade (wallet-sized entries).
- Success = **positive expectancy over a week**, not a fixed % per day.
- Losses stay small; streak-based sizing cuts after losses, modest boost after wins.

## What changed

| Area | Swing (legacy) | Compound (Path B) |
|------|----------------|-------------------|
| Timeframe | 1h | **15m** |
| TP / SL | +1.2% / −0.6% | **+0.4% / −0.25%** (ATR-scaled optional) |
| Allocation | 25% margin | **12%** margin |
| Thresholds | High (0.45 / 0.775) | **Lower (~0.38)**, capped search at 0.50 |
| Loop | 7s | **5s** |
| Trailing stop | Off | **On** (+0.4% activation, 0.2% trail) |
| Re-entry cooldown | None | **60s** after close |
| Session loss halt | −3% | **−5%** (weekly budget) |
| Sizing | Vol-scaled only | Vol-scaled + **win/loss streak multiplier** |

## First-time setup

```bash
# 1. Ensure .env has TRADING_PROFILE=COMPOUND (default in code)
# 2. Download 15m history + retrain (required — old 1h model won't match)
python data_pipeline.py
python model_brain.py          # v2.0.2+: thresholds require positive val PnL
python label_sweep.py          # optional: compare L1–L4 label configs first
python feature_sweep.py        # Phase 3: compare F0–F5 feature groups
python backtest_runner.py

# 3. Boot testnet
streamlit run dashboard.py
```

## Key env vars (`.env`)

```bash
TRADING_PROFILE=COMPOUND
INTERVAL=15m
CASH_ALLOCATION_PCT=0.12
LONG_THRESHOLD=0.38
SHORT_THRESHOLD=0.38
TRAILING_STOP_ENABLED=true
REENTRY_COOLDOWN_SECONDS=60
```

Revert to legacy swing: `TRADING_PROFILE=SWING` and retrain on 1h data.

## Dashboard

The **compound strip** shows 7-day PnL, trade count, expectancy, size multiplier,
and how far current signals are from threshold.
