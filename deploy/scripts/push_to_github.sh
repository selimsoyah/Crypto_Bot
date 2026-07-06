#!/usr/bin/env bash
# Create the private GitHub repo and push main (run once after `gh auth login`).
set -euo pipefail
cd "$(dirname "$0")/../.."
export PATH="${HOME}/.local/bin:${PATH}"

REPO_NAME="${1:-Crypto_Bot}"
VISIBILITY="${2:-private}"

if ! command -v gh >/dev/null 2>&1; then
  echo "Install GitHub CLI: https://cli.github.com/ (or use ~/.local/bin/gh)"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Not logged in. Run: gh auth login"
  exit 1
fi

if git remote get-url origin >/dev/null 2>&1; then
  echo "Remote 'origin' already exists:"
  git remote -v
else
  gh repo create "${REPO_NAME}" \
    --"${VISIBILITY}" \
    --source=. \
    --remote=origin \
    --description "BTC/USDT ML futures bot — COMPOUND 15m scalper (F2, testnet)"
fi

git push -u origin main
echo "Done. View repo: $(gh repo view --json url -q .url)"
