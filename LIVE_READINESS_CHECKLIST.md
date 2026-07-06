# Live-Readiness Checklist (Phase 3)

Evidence collected during the Phase 3 hardening pass. All items must pass
before proceeding to Phase 4 integration testing or Phase 5 staged rollout.

## 1. Secrets hygiene

| Check | Status | Evidence |
|-------|--------|----------|
| API keys loaded from env / `.env` only | ✅ | `config.py` `_env()` loader |
| Placeholder keys rejected at runtime | ✅ | `credentials_present()` |
| Secrets redacted from logs/alerts | ✅ | `config.sanitize_for_log()` |
| `.env.example` uses placeholders only | ✅ | `.env.example` (rotate any keys previously committed) |
| `.gitignore` excludes `.env`, lock files | ✅ | `.gitignore` |
| No secrets written to SQLite status log | ✅ | Manual audit — log columns are prices/PnL only |

**Action required:** If `.env.example` ever contained real keys, rotate them on Binance.

## 2. Execution venue clarity

| Check | Status | Evidence |
|-------|--------|----------|
| `EXECUTION_VENUE` config (TESTNET default) | ✅ | `config.EXECUTION_VENUE` |
| Startup validation blocks invalid venue | ✅ | `validate_execution_config()` |
| LIVE requires real credentials | ✅ | `test_live_requires_credentials` |
| Dashboard TESTNET/LIVE banner | ✅ | `_render_venue_banner()` in `dashboard.py` |
| LIVE boot requires typing "LIVE" | ✅ | Sidebar confirmation gate |
| Split data/exec documented in UI | ✅ | Banner shows data feed source |

Default: **`EXECUTION_VENUE=TESTNET`** — orders route to testnet only.

## 3. API resilience

| Check | Status | Evidence |
|-------|--------|----------|
| Exponential backoff on API calls | ✅ | `exchange_client.call_with_retry()` |
| Data pipeline klines use retry | ✅ | `data_pipeline._fetch_klines_with_retry` |
| Bot balance/orders use retry | ✅ | `bot_loop._get_usdt_balance`, `_futures_market_order` |
| Degraded state on repeated failures | ✅ | `BotState.connection_degraded` |
| New entries blocked when degraded | ✅ | `_open_position` gate |
| Mid-session reconnect attempted | ✅ | `_reconnect_client()` at 5 failures |
| Dashboard never silent-flat on API error | ✅ | `fetch_live_position()` returns `status: error` |

## 4. Alerting

| Check | Status | Evidence |
|-------|--------|----------|
| Alert module with log fallback | ✅ | `alerting.py` |
| Optional Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) | ✅ | `.env.example` |
| Rate limiting on duplicate alerts | ✅ | `ALERT_RATE_LIMIT_SECONDS` |
| Kill switch / session loss alerts | ✅ | `bot_loop._apply_risk_decision`, `activate_kill_switch` |
| Trade open/close alerts | ✅ | `_open_position`, `_close_position` |
| Iteration error alerts | ✅ | `_run` loop except handler |

## 5. Test evidence

```bash
cd /home/salim/Desktop/Crypto_Bot
source venv/bin/activate
pytest tests/ -q
```

Expected: all tests pass including:
- `tests/test_config_venue.py`
- `tests/test_exchange_retry.py`
- `tests/test_alerting.py`

## 6. Operator checklist before LIVE

- [ ] Phase 0–2 complete; model retrained after label alignment
- [ ] Phase 1 backtest reviewed (strategy profitable OOS after costs)
- [ ] `EXECUTION_VENUE=TESTNET` validated on testnet for ≥1 week
- [ ] Telegram alerts configured and tested
- [ ] Kill switch tested from dashboard
- [ ] Session loss limit tested (paper/testnet)
- [ ] Explicit sign-off to set `EXECUTION_VENUE=LIVE` (Phase 5 only)

---

**Phase 3 exit criteria:** secrets audit ✅ · venue banner ✅ · retry/reconnect ✅ · alerting ✅ · this checklist ✅

Say **"go Phase 4"** to proceed with full integration tests and dashboard honesty audit.

---

## Phase 4 — see `PHASE_4_CHECKLIST.md`

Integration tests, dashboard honesty fixes, and coverage tooling added in Phase 4.

---

## Phase 5 — see `STAGED_ROLLOUT_PROPOSAL.md`

Staged rollout proposal (documentation only). **No live execution enabled.**
Default remains `EXECUTION_VENUE=TESTNET`. Live requires explicit operator
sign-off and clearing of Stage 1 blockers (OOS profitability after retrain).

