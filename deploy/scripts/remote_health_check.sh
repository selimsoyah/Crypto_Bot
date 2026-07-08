#!/usr/bin/env bash
# Run ON the Oracle VM (or via SSH) to verify the bot is safe to leave unattended.
# Usage: bash deploy/scripts/remote_health_check.sh
set -euo pipefail

BOT_DIR="${BOT_DIR:-$HOME/Crypto_Bot}"
STALE_SEC="${STALE_SEC:-45}"
DB_FILE="${DB_FILE:-$BOT_DIR/bot_status_log.db}"

pass=0
warn=0
fail=0

ok()   { echo "  [PASS] $*"; pass=$((pass + 1)); }
note() { echo "  [WARN] $*"; warn=$((warn + 1)); }
bad()  { echo "  [FAIL] $*"; fail=$((fail + 1)); }

echo "==> Remote health check: $BOT_DIR"
echo ""

# --- Dashboard service -------------------------------------------------------
service_ok=false
if systemctl is-active --quiet crypto-bot-dashboard 2>/dev/null; then
  service_ok=true
elif sudo -n systemctl is-active --quiet crypto-bot-dashboard 2>/dev/null; then
  # Some hosts expose service state only via sudo in non-interactive shells.
  service_ok=true
fi
if [[ "$service_ok" == true ]]; then
  ok "systemd service crypto-bot-dashboard is active"
else
  note "could not confirm systemd active state for crypto-bot-dashboard"
fi

port_ok=false
if ss -ltn 2>/dev/null | grep -q ':8501 '; then
  port_ok=true
  ok "Streamlit listening on port 8501"
else
  note "port 8501 not detected (dashboard may still be starting)"
fi
if [[ "$service_ok" == false && "$port_ok" == false ]]; then
  bad "dashboard service appears down (systemd inactive + no port 8501 listener)"
fi

# --- Secrets & artifacts -----------------------------------------------------
for f in .env xgboost_trading_model.json decision_threshold.json; do
  if [[ -f "$BOT_DIR/$f" ]]; then
    ok "found $f"
  else
    bad "missing $BOT_DIR/$f"
  fi
done

if [[ -f "$BOT_DIR/.env" ]]; then
  perms="$(stat -c '%a' "$BOT_DIR/.env" 2>/dev/null || stat -f '%OLp' "$BOT_DIR/.env")"
  if [[ "$perms" == "600" ]]; then
    ok ".env permissions are 600"
  else
    note ".env permissions are $perms (recommended: chmod 600 .env)"
  fi
  if grep -qiE '^[[:space:]]*EXECUTION_VENUE[[:space:]]*=[[:space:]]*TESTNET' "$BOT_DIR/.env" 2>/dev/null; then
    ok "EXECUTION_VENUE=TESTNET"
  else
    venue="$(grep -iE '^[[:space:]]*EXECUTION_VENUE' "$BOT_DIR/.env" 2>/dev/null | tail -1 || true)"
    if [[ -n "$venue" ]]; then
      note "EXECUTION_VENUE is not TESTNET ($venue) — confirm this is intentional"
    else
      note "EXECUTION_VENUE not set in .env (code default is TESTNET)"
    fi
  fi
fi

# --- Kill switch / instance lock ---------------------------------------------
if [[ -f "$BOT_DIR/.bot_kill_switch" ]] || [[ -f "$BOT_DIR/.bot_risk_manual_resume_required" ]]; then
  bad "kill switch or manual-risk resume file is present — clear before leaving unattended"
else
  ok "no kill-switch / manual-resume flags on disk"
fi

if [[ -f "$BOT_DIR/.bot_instance.lock" ]]; then
  lock_pid="$(tr -d '[:space:]' < "$BOT_DIR/.bot_instance.lock" 2>/dev/null || true)"
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    ok "engine lock held by live PID $lock_pid"
  else
    note "lock file exists but PID ${lock_pid:-?} is not running — boot engine from dashboard"
  fi
else
  bad "no .bot_instance.lock — boot the engine from the dashboard (BOOT BOT ENGINE)"
fi

# --- Status log freshness (Python sqlite3 — no CLI required) ---------------
if [[ ! -f "$DB_FILE" ]]; then
  bad "no status database at $DB_FILE"
else
  log_result="$(
    DB_FILE="$DB_FILE" STALE_SEC="$STALE_SEC" python3 <<'PY'
import os
import sqlite3
from datetime import datetime, timezone

db = os.environ["DB_FILE"]
stale_sec = int(os.environ.get("STALE_SEC", "45"))
out = {"status": "empty", "age": -1, "ts": "", "action": "", "pos": "", "trades": 0}

try:
    con = sqlite3.connect(db)
    cur = con.cursor()
    row = cur.execute(
        "SELECT ts, action, open_position FROM status_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    trade_count = cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    out["trades"] = int(trade_count or 0)
    con.close()
except Exception as exc:
    print(f"ERROR|{exc}")
    raise SystemExit

if not row:
    print("EMPTY")
    raise SystemExit

ts_s, action, pos = row[0], row[1] or "", row[2] or ""
out["ts"], out["action"], out["pos"] = ts_s, action, pos
try:
    ts = datetime.strptime(str(ts_s).replace("+00:00", ""), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    age = int((datetime.now(timezone.utc) - ts).total_seconds())
except ValueError:
    print(f"BADTS|{ts_s}|{action}|{pos}|{out['trades']}")
    raise SystemExit

print(f"OK|{age}|{ts_s}|{action}|{pos}|{out['trades']}")
PY
  )"

  if [[ "$log_result" == EMPTY ]]; then
    bad "status_log is empty — engine may not be scanning"
  elif [[ "$log_result" == ERROR* ]]; then
    note "could not read status database: ${log_result#ERROR|}"
  elif [[ "$log_result" == BADTS* ]]; then
    IFS='|' read -r _ _ last_ts last_action last_pos trade_count <<< "$log_result"
    note "could not parse last log timestamp: $last_ts"
    echo "       last row: ts=$last_ts action=$last_action position=$last_pos"
    echo "       closed trades in ledger: ${trade_count:-0}"
  elif [[ "$log_result" == OK* ]]; then
    IFS='|' read -r _ age_sec last_ts last_action last_pos trade_count <<< "$log_result"
    echo "       last row: ts=$last_ts action=$last_action position=$last_pos"
    echo "       closed trades in ledger: $trade_count"
    if (( age_sec >= 0 && age_sec <= STALE_SEC )); then
      ok "status log updated ${age_sec}s ago (<= ${STALE_SEC}s)"
    else
      bad "status log stale (${age_sec}s old) — engine may be OFFLINE or stuck"
    fi
    if [[ "$last_action" == ERROR* ]] || [[ "$last_action" == *FAILED* ]]; then
      note "last action looks like an error: $last_action"
    fi
  fi
fi

echo ""
echo "----------------------------------------"
echo "Remote summary: $pass passed, $warn warnings, $fail failed"
echo "----------------------------------------"

if (( fail > 0 )); then
  exit 2
fi
if (( warn > 0 )); then
  exit 1
fi
exit 0
