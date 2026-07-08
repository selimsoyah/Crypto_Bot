#!/usr/bin/env bash
# Run FROM YOUR LOCAL PC when you sit down — prepares the laptop, checks Oracle,
# starts the SSH tunnel, and opens the dashboard in your browser.
#
# Usage:
#   bash deploy/scripts/startup_session.sh ubuntu@YOUR_ORACLE_IP
#   SSH_KEY=~/oracle-key.key bash deploy/scripts/startup_session.sh ubuntu@79.76.98.67
#   bash deploy/scripts/startup_session.sh -i ~/oracle-key.key ubuntu@79.76.98.67
#
# Options (env):
#   OPEN_BROWSER=0     — skip opening http://localhost:8501
#   START_DASHBOARD=1  — try sudo systemctl start on server if dashboard is down (default 1)
#   LOCAL_PORT=8501    — local tunnel port
#
# Exit codes: 0 = ready (engine live), 1 = tunnel up but review warnings, 2 = blocker
set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SSH_KEY="${SSH_KEY:-}"
SSH_EXTRA=()
LOCAL_PORT="${LOCAL_PORT:-8501}"
REMOTE_PORT="${REMOTE_PORT:-8501}"
REMOTE_DIR="${REMOTE_DIR:-~/Crypto_Bot}"
PID_FILE="$LOCAL_DIR/.dashboard_tunnel.pid"
START_DASHBOARD="${START_DASHBOARD:-1}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"

usage() {
  echo "Usage: $0 [-i ssh_key_file] ubuntu@ORACLE_PUBLIC_IP"
  echo "   or: SSH_KEY=~/oracle-key.key $0 ubuntu@ORACLE_PUBLIC_IP"
  echo ""
  echo "Stop tunnel later: bash deploy/scripts/stop_session.sh"
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
engine_needs_boot=false

loc_ok()   { echo "  [PASS] $*"; local_pass=$((local_pass + 1)); }
loc_note() { echo "  [WARN] $*"; local_warn=$((local_warn + 1)); }
loc_bad()  { echo "  [FAIL] $*"; local_fail=$((local_fail + 1)); }

echo "============================================================"
echo "  Crypto Bot — session startup ($SERVER)"
echo "============================================================"
echo ""

# --- Local prep --------------------------------------------------------------
echo "==> Local machine ($LOCAL_DIR)"

local_proc_running=false
if pgrep -af "python.*(bot_loop|dashboard\.py)" 2>/dev/null | grep -q "$LOCAL_DIR"; then
  local_proc_running=true
fi
if pgrep -af "streamlit run dashboard.py" 2>/dev/null | grep -q "$LOCAL_DIR"; then
  local_proc_running=true
fi

if [[ "$local_proc_running" == true ]]; then
  loc_bad "local bot/dashboard is running — stop it (FORCE SHUTDOWN) before using Oracle"
  pgrep -af "python.*(bot_loop|dashboard\.py)" 2>/dev/null | grep "$LOCAL_DIR" | sed 's/^/         /' || true
  pgrep -af "streamlit run dashboard.py" 2>/dev/null | grep "$LOCAL_DIR" | sed 's/^/         /' || true
else
  loc_ok "no local bot_loop.py / dashboard.py process detected"
fi

if [[ -f "$LOCAL_DIR/.bot_instance.lock" ]]; then
  lock_pid="$(tr -d '[:space:]' < "$LOCAL_DIR/.bot_instance.lock" 2>/dev/null || true)"
  if [[ "$local_proc_running" == true ]]; then
    loc_bad "local .bot_instance.lock present while bot is running"
  elif [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    loc_bad "local engine PID $lock_pid is still alive"
  else
    rm -f "$LOCAL_DIR/.bot_instance.lock"
    loc_ok "removed stale local .bot_instance.lock"
  fi
else
  loc_ok "no stale local engine lock"
fi

if ss -ltn 2>/dev/null | grep -q ":${LOCAL_PORT} " || \
   lsof -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN -t &>/dev/null; then
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    loc_ok "dashboard tunnel already listening on port ${LOCAL_PORT}"
    tunnel_already_up=true
  else
    loc_note "port ${LOCAL_PORT} is in use by another program — tunnel may fail"
    tunnel_already_up=false
  fi
else
  tunnel_already_up=false
fi

echo ""
echo "Local summary: $local_pass passed, $local_warn warnings, $local_fail failed"
echo ""

if (( local_fail > 0 )); then
  echo "============================================================"
  echo "  RESULT: BLOCKED — fix local [FAIL] items above first."
  echo "============================================================"
  exit 2
fi

# --- Remote reachability -----------------------------------------------------
echo "==> Oracle server ($SERVER)"
if ! "${ssh_cmd[@]}" "test -d $REMOTE_DIR"; then
  echo "  [FAIL] remote directory $REMOTE_DIR not found"
  echo ""
  echo "RESULT: BLOCKED — deploy the project to Oracle first (see deploy/ORACLE_DEPLOY.md)."
  exit 2
fi
echo "  [PASS] SSH reachable, project directory exists"

dashboard_active="$("${ssh_cmd[@]}" "systemctl is-active crypto-bot-dashboard 2>/dev/null || true")"
if [[ "$dashboard_active" != "active" ]]; then
  if [[ "$START_DASHBOARD" == "1" ]]; then
    echo "  [WARN] crypto-bot-dashboard is not active — attempting start..."
    if "${ssh_cmd[@]}" "sudo systemctl start crypto-bot-dashboard"; then
      echo "  [PASS] started crypto-bot-dashboard"
      sleep 3
    else
      echo "  [FAIL] could not start dashboard — run on server:"
      echo "         sudo systemctl start crypto-bot-dashboard"
      exit 2
    fi
  else
    echo "  [FAIL] crypto-bot-dashboard is not active"
    echo "         ssh $SERVER 'sudo systemctl start crypto-bot-dashboard'"
    exit 2
  fi
else
  echo "  [PASS] crypto-bot-dashboard is active"
fi

echo "  ... waiting for Streamlit on port ${REMOTE_PORT}"
ready=false
for _ in 1 2 3 4 5 6; do
  if "${ssh_cmd[@]}" "ss -ltn 2>/dev/null | grep -q ':${REMOTE_PORT} '"; then
    ready=true
    break
  fi
  sleep 2
done
if [[ "$ready" == true ]]; then
  echo "  [PASS] Streamlit listening on server port ${REMOTE_PORT}"
else
  echo "  [WARN] port ${REMOTE_PORT} not detected yet — tunnel may still work once it starts"
fi

echo ""
remote_rc=0
"${ssh_cmd[@]}" 'BOT_DIR="$HOME/Crypto_Bot" bash -s' \
  < "$LOCAL_DIR/deploy/scripts/remote_health_check.sh" || remote_rc=$?

if (( remote_rc >= 2 )); then
  engine_needs_boot=true
fi

# --- SSH tunnel --------------------------------------------------------------
echo ""
echo "==> Dashboard tunnel"
if [[ "${tunnel_already_up:-false}" == true ]]; then
  echo "  [PASS] reusing existing tunnel (PID $(cat "$PID_FILE"))"
else
  ssh -f -N "${SSH_EXTRA[@]}" \
    -o ExitOnForwardFailure=yes \
    -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
    "$SERVER"

  tunnel_pid=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    tunnel_pid="$(pgrep -f "ssh.*-L ${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}.*${SERVER}" 2>/dev/null | head -1 || true)"
    if [[ -n "$tunnel_pid" ]]; then
      break
    fi
    sleep 0.5
  done

  if [[ -z "$tunnel_pid" ]]; then
    echo "  [FAIL] SSH tunnel did not start"
    exit 2
  fi

  echo "$tunnel_pid" > "$PID_FILE"
  echo "  [PASS] tunnel running (PID $tunnel_pid) → http://localhost:${LOCAL_PORT}"
fi

# --- Browser -----------------------------------------------------------------
if [[ "$OPEN_BROWSER" == "1" ]]; then
  url="http://localhost:${LOCAL_PORT}"
  if command -v xdg-open &>/dev/null; then
    xdg-open "$url" &>/dev/null &
    echo "  [PASS] opened $url in browser"
  elif command -v sensible-browser &>/dev/null; then
    sensible-browser "$url" &>/dev/null &
    echo "  [PASS] opened $url in browser"
  else
    echo "  [WARN] could not auto-open browser — visit $url manually"
  fi
fi

# --- Final verdict -----------------------------------------------------------
echo ""
if (( remote_rc == 0 )); then
  echo "============================================================"
  echo "  RESULT: READY — engine is LIVE on Oracle."
  echo "============================================================"
  echo ""
  echo "Dashboard: http://localhost:${LOCAL_PORT}"
  echo "Stop tunnel: bash deploy/scripts/stop_session.sh"
  echo "Before powering off: bash deploy/scripts/preflight_shutdown.sh $SERVER"
  exit 0
fi

echo "============================================================"
echo "  RESULT: TUNNEL UP — review Oracle status above."
echo "============================================================"
echo ""
if [[ "$engine_needs_boot" == true ]]; then
  echo "Next step: open the dashboard sidebar → 🚀 BOOT BOT ENGINE"
  echo "(Required after an Oracle VM reboot; dashboard auto-starts, engine does not.)"
  echo ""
fi
echo "Dashboard: http://localhost:${LOCAL_PORT}"
echo "Stop tunnel: bash deploy/scripts/stop_session.sh"
echo "When done for the day: bash deploy/scripts/preflight_shutdown.sh $SERVER"
exit 1
