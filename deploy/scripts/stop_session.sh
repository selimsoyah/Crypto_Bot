#!/usr/bin/env bash
# Stop the background SSH dashboard tunnel started by startup_session.sh.
#
# Usage: bash deploy/scripts/stop_session.sh
set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PID_FILE="$LOCAL_DIR/.dashboard_tunnel.pid"
LOCAL_PORT="${LOCAL_PORT:-8501}"

stopped=false

if [[ -f "$PID_FILE" ]]; then
  pid="$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 0.5
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "Stopped dashboard tunnel (PID $pid)."
    stopped=true
  fi
  rm -f "$PID_FILE"
fi

if [[ "$stopped" == false ]]; then
  orphan="$(pgrep -f "ssh.*-L ${LOCAL_PORT}:127.0.0.1:${LOCAL_PORT}" 2>/dev/null | head -1 || true)"
  if [[ -n "$orphan" ]]; then
    kill "$orphan" 2>/dev/null || true
    echo "Stopped orphan SSH tunnel (PID $orphan)."
    stopped=true
  fi
fi

if [[ "$stopped" == false ]]; then
  echo "No dashboard tunnel found on port ${LOCAL_PORT}."
fi
