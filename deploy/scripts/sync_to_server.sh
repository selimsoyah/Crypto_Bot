#!/usr/bin/env bash
# Run FROM YOUR LOCAL PC to upload the project to Oracle VM.
# Usage: bash deploy/scripts/sync_to_server.sh ubuntu@123.45.67.89
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 ubuntu@YOUR_ORACLE_PUBLIC_IP"
  echo "Example: $0 ubuntu@129.154.123.45"
  exit 1
fi

SERVER="$1"
LOCAL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
REMOTE_DIR="~/Crypto_Bot"

echo "==> Syncing $LOCAL_DIR to $SERVER:$REMOTE_DIR"
echo "    (excluding venv, cache, local database — .env copied separately if present)"

RSYNC_EXCLUDES=(
  --exclude 'venv/'
  --exclude '__pycache__/'
  --exclude '.pytest_cache/'
  --exclude '.git/'
  --exclude '*.db'
  --exclude '*.db-wal'
  --exclude '*.db-shm'
  --exclude '.bot_instance.lock'
  --exclude 'session_exports/'
  --exclude '.env'
)

rsync -avz --progress "${RSYNC_EXCLUDES[@]}" "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"

echo ""
echo "==> Upload .env separately (secrets — never commit to git):"
echo "    scp $LOCAL_DIR/.env $SERVER:~/Crypto_Bot/.env"
echo "    ssh $SERVER 'chmod 600 ~/Crypto_Bot/.env'"
echo ""
echo "==> Then on the server (first time only):"
echo "    ssh $SERVER 'cd ~/Crypto_Bot && bash deploy/scripts/install_server.sh'"
echo ""
echo "==> Or if already installed, restart dashboard:"
echo "    ssh $SERVER 'sudo systemctl restart crypto-bot-dashboard'"
