#!/usr/bin/env bash
# Run FROM YOUR LOCAL PC before powering off — verifies Oracle VM is ready
# to run the bot unattended and that no local engine is competing.
#
# Usage:
#   bash deploy/scripts/preflight_shutdown.sh ubuntu@YOUR_ORACLE_IP
#   SSH_KEY=~/oracle-key.key bash deploy/scripts/preflight_shutdown.sh ubuntu@79.76.98.67
#   bash deploy/scripts/preflight_shutdown.sh -i ~/oracle-key.key ubuntu@79.76.98.67
#
# Exit codes: 0 = safe to power off, 1 = warnings only, 2 = blockers found
set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SSH_KEY="${SSH_KEY:-}"
SSH_EXTRA=()

usage() {
  echo "Usage: $0 [-i ssh_key_file] ubuntu@ORACLE_PUBLIC_IP"
  echo "   or: SSH_KEY=~/oracle-key.key $0 ubuntu@ORACLE_PUBLIC_IP"
  exit 1
}

while getopts ":i:h" opt; do
  case "$opt" in
    i) SSH_KEY="$OPTARG" ;;
    h) usage ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))

if [[ $# -lt 1 ]]; then
  usage
fi

SERVER="$1"
REMOTE_DIR="${REMOTE_DIR:-~/Crypto_Bot}"

if [[ -n "$SSH_KEY" ]]; then
  if [[ ! -f "$SSH_KEY" ]]; then
    echo "SSH key not found: $SSH_KEY"
    exit 2
  fi
  SSH_EXTRA=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)
fi

ssh_cmd=(ssh "${SSH_EXTRA[@]}" "$SERVER")
local_pass=0
local_warn=0
local_fail=0

loc_ok()   { echo "  [PASS] $*"; local_pass=$((local_pass + 1)); }
loc_note() { echo "  [WARN] $*"; local_warn=$((local_warn + 1)); }
loc_bad()  { echo "  [FAIL] $*"; local_fail=$((local_fail + 1)); }

echo "============================================================"
echo "  Crypto Bot — pre-shutdown readiness ($SERVER)"
echo "============================================================"
echo ""

# --- Local checks ------------------------------------------------------------
echo "==> Local machine ($LOCAL_DIR)"

local_proc_running=false
if pgrep -af "python.*(bot_loop|dashboard\.py)" 2>/dev/null | grep -q "$LOCAL_DIR"; then
  local_proc_running=true
fi
if pgrep -af "streamlit run dashboard.py" 2>/dev/null | grep -q "$LOCAL_DIR"; then
  local_proc_running=true
fi

if [[ -f "$LOCAL_DIR/.bot_instance.lock" ]]; then
  lock_pid="$(tr -d '[:space:]' < "$LOCAL_DIR/.bot_instance.lock" 2>/dev/null || true)"
  if [[ "$local_proc_running" == true ]]; then
    loc_bad "local bot is running — use FORCE SHUTDOWN before leaving Oracle unattended"
  elif [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    loc_bad "local engine PID $lock_pid is still alive — stop it before powering off"
  else
    loc_note "stale .bot_instance.lock (no local bot running) — safe to remove:"
    echo "         rm -f $LOCAL_DIR/.bot_instance.lock"
    if [[ "${CLEAN_STALE_LOCK:-}" == "1" ]]; then
      rm -f "$LOCAL_DIR/.bot_instance.lock"
      loc_ok "removed stale local .bot_instance.lock"
    fi
  fi
else
  loc_ok "no local engine lock (.bot_instance.lock)"
fi

if [[ "$local_proc_running" == true ]]; then
  loc_bad "local bot/dashboard Python process still running for this project"
  pgrep -af "python.*(bot_loop|dashboard\.py)" 2>/dev/null | grep "$LOCAL_DIR" | sed 's/^/         /' || true
  pgrep -af "streamlit run dashboard.py" 2>/dev/null | grep "$LOCAL_DIR" | sed 's/^/         /' || true
else
  loc_ok "no local bot_loop.py / dashboard.py process detected"
fi

echo ""
echo "Local summary: $local_pass passed, $local_warn warnings, $local_fail failed"
echo ""

# --- Remote checks -----------------------------------------------------------
echo "==> Oracle server ($SERVER)"
if ! "${ssh_cmd[@]}" "test -d $REMOTE_DIR"; then
  echo "  [FAIL] remote directory $REMOTE_DIR not found"
  echo ""
  echo "RESULT: NOT READY — fix failures above before powering off."
  exit 2
fi

remote_rc=0
"${ssh_cmd[@]}" 'BOT_DIR="$HOME/Crypto_Bot" bash -s' \
  < "$LOCAL_DIR/deploy/scripts/remote_health_check.sh" || remote_rc=$?

echo ""

# --- Final verdict -----------------------------------------------------------
total_fail=$((local_fail + (remote_rc >= 2 ? 1 : 0)))
total_warn=$((local_warn + (remote_rc == 1 ? 1 : 0)))

if (( local_fail > 0 || remote_rc >= 2 )); then
  echo "============================================================"
  echo "  RESULT: NOT READY — fix [FAIL] items before powering off."
  echo "============================================================"
  echo ""
  echo "Quick fixes:"
  echo "  • Stale local lock: rm -f $LOCAL_DIR/.bot_instance.lock"
  echo "  • Or re-run with: CLEAN_STALE_LOCK=1 SSH_KEY=... $0 $SERVER"
  echo "  • Boot engine on server: SSH tunnel → http://localhost:8501 → BOOT BOT ENGINE"
  echo "  • Dashboard service:  ssh $SERVER 'sudo systemctl start crypto-bot-dashboard'"
  exit 2
fi

if (( total_warn > 0 )); then
  echo "============================================================"
  echo "  RESULT: PROBABLY OK — review [WARN] items, then you may power off."
  echo "============================================================"
  echo ""
  echo "You do NOT need the SSH tunnel or browser open while away."
  echo "Re-check later: SSH_KEY=... $0 $SERVER"
  exit 1
fi

echo "============================================================"
echo "  RESULT: SAFE TO POWER OFF YOUR PC"
echo "============================================================"
echo ""
echo "The Oracle VM will keep running the dashboard + trading engine."
echo "You do NOT need the SSH tunnel or browser open while away."
echo ""
echo "Remember:"
echo "  • After an Oracle VM reboot you must BOOT the engine again."
echo "  • To monitor later: bash deploy/scripts/open_dashboard.sh $SERVER"
exit 0
