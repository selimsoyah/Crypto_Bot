#!/usr/bin/env bash
# Run ON the Oracle VM (after uploading Crypto_Bot) as the ubuntu user.
# Usage: cd ~/Crypto_Bot && bash deploy/scripts/install_server.sh
set -euo pipefail

BOT_DIR="${BOT_DIR:-$HOME/Crypto_Bot}"
cd "$BOT_DIR"

echo "==> Installing OS packages (may prompt for sudo password)..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-venv python3-pip git rsync ufw

echo "==> Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing Streamlit config (localhost only)..."
mkdir -p "$HOME/.streamlit"
cp deploy/streamlit/config.toml "$HOME/.streamlit/config.toml"

if [[ ! -f .env ]]; then
  echo ""
  echo "WARNING: .env not found. Copy it from your PC before starting:"
  echo "  scp .env ubuntu@YOUR_SERVER_IP:~/Crypto_Bot/.env"
  echo "  chmod 600 ~/Crypto_Bot/.env"
  cp .env.example .env
  echo "  (Created .env from .env.example — edit with real keys!)"
fi
chmod 600 .env 2>/dev/null || true

mkdir -p session_exports

echo "==> Installing systemd services (dashboard + headless engine)..."
sudo cp deploy/systemd/crypto-bot-dashboard.service /etc/systemd/system/
sudo cp deploy/systemd/crypto-bot.service /etc/systemd/system/
sudo sed -i "s|/home/ubuntu|$HOME|g" /etc/systemd/system/crypto-bot-dashboard.service
sudo sed -i "s|/home/ubuntu|$HOME|g" /etc/systemd/system/crypto-bot.service
sudo cp deploy/sudoers/crypto-bot-dashboard /etc/sudoers.d/crypto-bot-dashboard
sudo chmod 440 /etc/sudoers.d/crypto-bot-dashboard
sudo systemctl daemon-reload
sudo systemctl enable crypto-bot-dashboard.service
sudo systemctl enable crypto-bot.service

echo "==> Firewall: allow SSH only (dashboard accessed via SSH tunnel)..."
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
echo "y" | sudo ufw enable || true

echo ""
echo "==> Install complete."
echo "Next steps:"
echo "  1. Edit ~/Crypto_Bot/.env with your TESTNET API keys"
echo "  2. sudo systemctl start crypto-bot-dashboard"
echo "  3. sudo systemctl start crypto-bot"
echo "  4. From your PC: bash deploy/scripts/open_dashboard.sh ubuntu@YOUR_SERVER_IP"
echo "  5. Open http://localhost:8501 (engine status syncs from headless service)"
