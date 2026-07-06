#!/usr/bin/env bash
# Run FROM YOUR LOCAL PC — opens an SSH tunnel so the remote dashboard
# appears at http://localhost:8501 (same as running Streamlit locally).
#
# Usage: bash deploy/scripts/open_dashboard.sh ubuntu@YOUR_ORACLE_PUBLIC_IP
#
# Keep this terminal open while using the dashboard. Press Ctrl+C to close the tunnel.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 ubuntu@YOUR_ORACLE_PUBLIC_IP"
  exit 1
fi

SERVER="$1"
LOCAL_PORT="${LOCAL_PORT:-8501}"
REMOTE_PORT="${REMOTE_PORT:-8501}"

echo "==> SSH tunnel: localhost:${LOCAL_PORT} -> ${SERVER}:${REMOTE_PORT}"
echo "    Open in your browser: http://localhost:${LOCAL_PORT}"
echo "    Boot the bot from the sidebar (same as on your PC)."
echo "    Press Ctrl+C to close the tunnel."
echo ""

ssh -N -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "$SERVER"
