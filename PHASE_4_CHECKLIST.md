# Phase 4 — Testing & Dashboard Honesty Checklist

Exit criteria from the hardening audit: **`pytest` clean** + coverage on core
decision modules + dashboard metrics that do not mislead operators.

## 1. Unit test coverage

| Module | Test file | Status |
|--------|-----------|--------|
| Labeling / TP-SL alignment | `test_labeling.py` | ✅ Phase 0 |
| State machine (open/flip/SL/TP) | `test_state_machine.py` | ✅ Phase 0 |
| Risk circuit breakers | `test_risk_engine.py` | ✅ Phase 2 |
| `decide_direction` edge cases | `test_decide_direction.py` | ✅ Phase 4 |
| Feature determinism | `test_feature_factory.py` | ✅ Phase 4 |
| Order sizing / fill price | `test_order_sizing.py` | ✅ Phase 4 |
| Dashboard stats honesty | `test_dashboard_stats.py` | ✅ Phase 4 |
| Venue / retry / alerting | `test_config_venue.py`, etc. | ✅ Phase 3 |

## 2. Integration tests (mocked exchange)

| Scenario | Test | Status |
|----------|------|--------|
| Full lifecycle open → hold → TP close | `test_full_lifecycle_open_hold_close` | ✅ |
| Degraded API blocks new entries | `test_connection_degraded_blocks_new_entry` | ✅ |
| Brackets still close when degraded | `test_bracket_tp_still_closes_when_degraded` | ✅ |
| Consecutive-loss pause | `test_consecutive_loss_pause_blocks_entry` | ✅ |
| Empty candles → degraded | `test_empty_candles_sets_degraded` | ✅ |
| Insufficient balance → blocked | `test_insufficient_balance_blocks_order` | ✅ |
| Signal flip (close + open rows) | `test_state_machine.py` | ✅ |

## 3. Dashboard honesty fixes

| Issue | Fix | Status |
|-------|-----|--------|
| PnL strip ignored exchange | `reconcile_floating_pnl()` prefers exchange API | ✅ |
| Exchange error showed as flat | `status: error` + stale log warning | ✅ |
| Session risk PnL ignored unrealized | `compute_session_risk_pnl()` | ✅ |
| Hardcoded "25% alloc" | Vol-scaled label from risk snapshot | ✅ |
| Win rate TP-only | Counts TP **or** positive `realized_pnl` | ✅ |
| Empty log silent zeros | `data_warnings` banner on main page | ✅ |
| Threshold panel | Already uses `resolve_live_thresholds()` | ✅ Phase 0 |

Pure logic extracted to **`dashboard_stats.py`** for testability without importing Streamlit.

## 4. Run tests

```bash
cd /home/salim/Desktop/Crypto_Bot
source venv/bin/activate
pip install pytest-cov   # if not installed
pytest tests/ -q
```

## 5. Coverage report (exit evidence)

```bash
pytest tests/ \
  --cov=risk_engine \
  --cov=bot_loop \
  --cov=feature_factory \
  --cov=dashboard_stats \
  --cov-report=term-missing
```

Target: no failing tests; core modules show meaningful line coverage on
decision paths (risk gates, `_iteration`, labeling, stats helpers).

## 6. Before Phase 5

- [ ] All tests green
- [ ] Coverage report reviewed
- [ ] Dashboard verified manually (TESTNET banner, PnL source labels)
- [ ] Phase 1 backtest still unprofitable — do not go LIVE until retrained

---

**Phase 4 exit criteria met when:** `pytest tests/ -q` passes and this checklist is complete.

Say **"go Phase 5"** for the staged rollout proposal (documentation only — no live switch without sign-off).
