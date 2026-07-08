#!/usr/bin/env bash
# Run FROM YOUR LOCAL PC — opens an SSH tunnel so the remote dashboard
# appears at http://localhost:8501 (same as running Streamlit locally).
#
# Usage:
#   bash deploy/scripts/open_dashboard.sh ubuntu@YOUR_ORACLE_PUBLIC_IP
#   SSH_KEY=~/oracle-key.key bash deploy/scripts/open_dashboard.sh ubuntu@79.76.98.67
#
# For full morning setup (health checks + tunnel + browser), use startup_session.sh instead.
# Keep this terminal open while using the dashboard. Press Ctrl+C to close the tunnel.
set -euo pipefail

SSH_KEY="${SSH_KEY:-}"
SSH_EXTRA=()

usage() {
  echo "Usage: $0 [-i ssh_key_file] ubuntu@YOUR_ORACLE_PUBLIC_IP"
  echo "   or: SSH_KEY=~/oracle-key.key $0 ubuntu@YOUR_ORACLE_PUBLIC_IP"
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
LOCAL_PORT="${LOCAL_PORT:-8501}"
REMOTE_PORT="${REMOTE_PORT:-8501}"

if [[ -n "$SSH_KEY" ]]; then
  if [[ ! -f "$SSH_KEY" ]]; then
    echo "SSH key not found: $SSH_KEY"
    exit 1
  fi
  SSH_EXTRA=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)
fi

echo "==> SSH tunnel: localhost:${LOCAL_PORT} -> ${SERVER}:${REMOTE_PORT}"
echo "    Open in your browser: http://localhost:${LOCAL_PORT}"
echo "    Boot the bot from the sidebar (same as on your PC)."
echo "    Press Ctrl+C to close the tunnel."
echo ""

ssh -N "${SSH_EXTRA[@]}" -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "$SERVER"
