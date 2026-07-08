#!/usr/bin/env bash
# Run FROM YOUR LOCAL PC to upload the project to Oracle VM.
#
# Usage:
#   bash deploy/scripts/sync_to_server.sh ubuntu@YOUR_ORACLE_PUBLIC_IP
#   SSH_KEY=~/oracle-key.key bash deploy/scripts/sync_to_server.sh ubuntu@79.76.98.67
#   bash deploy/scripts/sync_to_server.sh -i ~/oracle-key.key ubuntu@79.76.98.67
set -euo pipefail

SSH_KEY="${SSH_KEY:-}"
SSH_RSYNC=()

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
LOCAL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
REMOTE_DIR="~/Crypto_Bot"

if [[ -n "$SSH_KEY" ]]; then
  if [[ ! -f "$SSH_KEY" ]]; then
    echo "SSH key not found: $SSH_KEY"
    exit 1
  fi
  SSH_RSYNC=(-e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new")
fi

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

rsync -avz --progress "${SSH_RSYNC[@]}" "${RSYNC_EXCLUDES[@]}" \
  "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"

echo ""
echo "==> Upload .env separately (secrets — never commit to git):"
if [[ -n "$SSH_KEY" ]]; then
  echo "    scp -i $SSH_KEY $LOCAL_DIR/.env $SERVER:~/Crypto_Bot/.env"
  echo "    ssh -i $SSH_KEY $SERVER 'chmod 600 ~/Crypto_Bot/.env'"
else
  echo "    scp $LOCAL_DIR/.env $SERVER:~/Crypto_Bot/.env"
  echo "    ssh $SERVER 'chmod 600 ~/Crypto_Bot/.env'"
fi
echo ""
echo "==> Then on the server (first time only):"
echo "    ssh $SERVER 'cd ~/Crypto_Bot && bash deploy/scripts/install_server.sh'"
echo ""
echo "==> Or if already installed, restart dashboard:"
if [[ -n "$SSH_KEY" ]]; then
  echo "    ssh -i $SSH_KEY $SERVER 'sudo systemctl restart crypto-bot-dashboard'"
else
  echo "    ssh $SERVER 'sudo systemctl restart crypto-bot-dashboard'"
fi
