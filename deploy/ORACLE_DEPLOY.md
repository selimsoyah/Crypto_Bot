# Oracle Cloud Always Free — Deployment Guide

Run the Crypto Bot **24/7 on Oracle Cloud** (free tier) while using the **same Streamlit dashboard** you use on your PC. The dashboard listens on **localhost only** on the server; you open it from your laptop via an **SSH tunnel** — secure and free.

**What you get**

- Bot + dashboard run on Oracle VM continuously
- You control it exactly like at home: open browser → **BOOT BOT ENGINE**
- TESTNET only by default (`EXECUTION_VENUE=TESTNET`)
- Session CSV exports in `~/Crypto_Bot/session_exports/`

**Time required:** ~45–90 minutes first time (Oracle signup + VM create + upload)

---

## Overview (5 phases)


| Phase | Where           | What                                   |
| ----- | --------------- | -------------------------------------- |
| **1** | Browser         | Create Oracle account + Ubuntu VM      |
| **2** | Your PC         | Upload project + `.env`                |
| **3** | Oracle VM (SSH) | Install Python deps + systemd service  |
| **4** | Your PC         | SSH tunnel → `http://localhost:8501`   |
| **5** | Browser         | Boot bot from sidebar (usual workflow) |


---

## Phase 1 — Create the Oracle Always Free VM

### 1.1 Sign up

1. Go to [https://www.oracle.com/cloud/free/](https://www.oracle.com/cloud/free/)
2. Create an account (credit card may be required for verification; Always Free resources stay **$0** if you stay within limits).
3. Sign in to **Oracle Cloud Console**: [https://cloud.oracle.com](https://cloud.oracle.com)

### 1.2 Create a compartment (optional but tidy)

1. Menu **☰** → **Identity & Security** → **Compartments**
2. **Create Compartment** → name e.g. `crypto-bot` → Create

### 1.3 Networking — allow SSH from your IP

1. **☰** → **Networking** → **Virtual cloud networks**
2. Open your **default VCN** (or create one with wizard: VCN + internet gateway + public subnet)
3. Click the **Security List** for your public subnet
4. **Add Ingress Rules**:

  | Source CIDR         | Protocol | Dest port | Description    |
  | ------------------- | -------- | --------- | -------------- |
  | **Your home IP/32** | TCP      | 22        | SSH from my PC |

   Find your IP: [https://ifconfig.me](https://ifconfig.me) — use e.g. `203.0.113.10/32`
   **Do not** open port 8501 to the world. The dashboard uses an SSH tunnel.

### 1.4 Create the compute instance

1. **☰** → **Compute** → **Instances** → **Create instance**
2. **Name:** `crypto-bot-vm`
3. **Placement:** keep default AD
4. **Image:** **Ubuntu 22.04** or **24.04** (Canonical)
5. **Shape:** Click **Change shape**
  - **Ampere (ARM)** → `VM.Standard.A1.Flex` → **1 OCPU**, **6 GB RAM** (recommended, plenty for this bot)
  - *Or* **AMD** → `VM.Standard.E2.1.Micro` (Always Free eligible)
6. **Networking:** Public subnet, **Assign public IPv4 address** = Yes
7. **SSH keys:** **Generate a key pair** → **Save private key** (`oracle_key.pem`) — you need this to connect
8. **Boot volume:** default 50 GB is fine
9. **Create**
10. Wait until state **Running**. Note the **Public IP address** (e.g. `129.154.123.45`).

### 1.5 First SSH login (from your PC)

```bash
chmod 600 ~/Downloads/oracle_key.pem   # path to your saved key

ssh -i ~/Downloads/oracle_key.pem ubuntu@YOUR_PUBLIC_IP
```

Default user on Ubuntu Oracle images: `**ubuntu**`

If connection fails, check: instance Running, correct IP, security list allows SSH from your IP, key permissions `600`.

---

## Phase 2 — Migrate the project from your PC

**On your PC** (in the project folder):

### 2.1 Stop the local bot (important)

Only **one** engine should run per testnet account.

1. In local Streamlit: sidebar → **🛑 FORCE SHUTDOWN**
2. Stop local Streamlit (`Ctrl+C` in terminal)

### 2.2 Upload code + artifacts

```bash
cd /home/salim/Desktop/Crypto_Bot

# Make scripts executable (once)
chmod +x deploy/scripts/*.sh

# Sync project (excludes venv, local .db, .env)
bash deploy/scripts/sync_to_server.sh ubuntu@YOUR_PUBLIC_IP
```

If you use Oracle’s SSH key:

```bash
rsync -avz -e "ssh -i ~/Downloads/oracle_key.pem" \
  --exclude venv --exclude __pycache__ --exclude '.pytest_cache' \
  --exclude '.git' --exclude '*.db' --exclude '.env' \
  /home/salim/Desktop/Crypto_Bot/ \
  ubuntu@YOUR_PUBLIC_IP:~/Crypto_Bot/
```

### 2.3 Upload secrets and optional data

```bash
# API keys (required)
scp -i ~/Downloads/oracle_key.pem .env ubuntu@YOUR_PUBLIC_IP:~/Crypto_Bot/.env

# Model + thresholds (recommended — skip retrain on server)
scp -i ~/Downloads/oracle_key.pem xgboost_trading_model.json decision_threshold.json ubuntu@YOUR_PUBLIC_IP:~/Crypto_Bot/

# 15m training cache (optional — saves download time)
scp -i ~/Downloads/oracle_key.pem historical_btc_15m.parquet ubuntu@YOUR_PUBLIC_IP:~/Crypto_Bot/

ssh -i ~/Downloads/oracle_key.pem ubuntu@YOUR_PUBLIC_IP 'chmod 600 ~/Crypto_Bot/.env'
```

**Files you should have on the server**


| File                          | Required?                |
| ----------------------------- | ------------------------ |
| `.env`                        | ✅                        |
| `xgboost_trading_model.json`  | ✅ (or retrain on server) |
| `decision_threshold.json`     | ✅                        |
| `historical_btc_15m.parquet`  | Optional                 |
| `requirements.txt`, all `.py` | ✅ (via rsync)            |


---

## Phase 3 — Install on the server (SSH)

SSH into the VM:

```bash
ssh -i ~/Downloads/oracle_key.pem ubuntu@YOUR_PUBLIC_IP
```

Run the installer:

```bash
cd ~/Crypto_Bot
bash deploy/scripts/install_server.sh
```

This will:

- Install `python3`, `venv`, `git`, `ufw`
- Create `venv` and `pip install -r requirements.txt`
- Copy Streamlit config (**bind 127.0.0.1 only**)
- Install **systemd** service `crypto-bot-dashboard` (starts on boot)
- Enable firewall: **SSH only**

### 3.1 Verify `.env` on server

```bash
nano ~/Crypto_Bot/.env
```

Ensure at minimum:

```bash
EXECUTION_VENUE=TESTNET
TRADING_PROFILE=COMPOUND
API_KEY=your_testnet_key
SECRET_KEY=your_testnet_secret
```

Optional Telegram:

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### 3.2 (Optional) Retrain on server instead of copying model

Only if you did **not** copy the model files:

```bash
cd ~/Crypto_Bot
source venv/bin/activate
python model_brain.py    # downloads 15m data — takes several minutes
```

### 3.3 Start the dashboard service

```bash
sudo systemctl start crypto-bot-dashboard
sudo systemctl status crypto-bot-dashboard
```

You should see **active (running)**.

Logs:

```bash
journalctl -u crypto-bot-dashboard -f
```

---

## Phase 4 — Use the dashboard from your PC (like usual)

**Keep the Oracle VM service running.** On **your PC**, open a **new terminal**:

```bash
cd /home/salim/Desktop/Crypto_Bot
bash deploy/scripts/open_dashboard.sh ubuntu@YOUR_PUBLIC_IP
```

With Oracle key:

```bash
ssh -i ~/Downloads/oracle_key.pem -N -L 8501:127.0.0.1:8501 ubuntu@YOUR_PUBLIC_IP
```

Leave this terminal open. In your browser:

### 👉 [http://localhost:8501](http://localhost:8501)

You should see the same **BTC/USDT ML Futures Desk** UI.

---

## Phase 5 — Start the bot (same as home)

1. Confirm banner: `EXECUTION: TESTNET | … 15m … | COMPOUND`
2. Sidebar → **🚀 BOOT BOT ENGINE**
3. Heartbeat should go **BOOTING** → **LIVE** within ~10–30 seconds
4. Wallet ~$5,000 (testnet), status log updating every ~5s

**Stop / shutdown**

- Sidebar → **🛑 FORCE SHUTDOWN** (generates PDF + CSV in `session_exports/` on server)
- To download session CSV to your PC:

```bash
scp -i ~/Downloads/oracle_key.pem ubuntu@YOUR_PUBLIC_IP:~/Crypto_Bot/session_exports/*.csv ~/Downloads/
```

---

## Day‑to‑day operations


| Task                         | Command                                                                                              |
| ---------------------------- | ---------------------------------------------------------------------------------------------------- |
| Open dashboard from PC       | `bash deploy/scripts/open_dashboard.sh ubuntu@IP`                                                    |
| Dashboard status (on server) | `sudo systemctl status crypto-bot-dashboard`                                                         |
| Restart dashboard            | `sudo systemctl restart crypto-bot-dashboard`                                                        |
| View dashboard logs          | `journalctl -u crypto-bot-dashboard -f`                                                              |
| Update code from PC          | `bash deploy/scripts/sync_to_server.sh ubuntu@IP` then `sudo systemctl restart crypto-bot-dashboard` |
| Reboot VM                    | Dashboard auto-starts; **boot bot again** from sidebar                                               |
| SSH to server                | `ssh -i key.pem ubuntu@IP`                                                                           |


---

## Auto-start after VM reboot

The **dashboard** starts automatically via systemd.

The **trading engine** does **not** auto-boot (same as your PC — you click BOOT). After a server reboot:

1. SSH tunnel from PC
2. Open `http://localhost:8501`
3. Click **BOOT BOT ENGINE**

*Optional:* if you want the engine to start without the dashboard, run headless `python bot_loop.py` under a separate systemd unit — not the default “like usual” workflow.

---

## Security checklist

- `.env` chmod `600` on server
- Firewall: **only SSH (22)** from your IP — not 8501 public
- `EXECUTION_VENUE=TESTNET` until backtest gates pass
- Never commit `.env` or API keys to git
- Stop local bot when server bot is running
- Rotate API keys if they were ever exposed

---

## Troubleshooting


| Problem                                | Fix                                                                                                  |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| SSH timeout                            | Check Oracle security list (port 22), your IP changed?, instance Running                             |
| `Another bot instance already running` | On server: `rm -f ~/Crypto_Bot/.bot_instance.lock` after stopping bot; ensure PC bot is off          |
| Dashboard tunnel refused               | `sudo systemctl start crypto-bot-dashboard`; check `journalctl -u crypto-bot-dashboard`              |
| ENGINE STALE                           | Force shutdown → boot again; check `journalctl` for errors                                           |
| Binance API errors                     | Verify `.env` keys; testnet keys from [testnet.binancefuture.com](https://testnet.binancefuture.com) |
| Out of memory                          | Use A1 Flex with 6 GB RAM; avoid running retrain while bot is live                                   |
| Oracle signup rejected                 | Try different region/card; support forum / retry next day                                            |


---

## Cost

**Oracle Always Free** (within limits): **$0/month** for the VM shape described.

You pay nothing extra unless you create paid resources outside Always Free.

---

## Quick command cheat sheet

**PC — upload everything**

```bash
bash deploy/scripts/sync_to_server.sh ubuntu@YOUR_PUBLIC_IP
scp .env xgboost_trading_model.json decision_threshold.json ubuntu@YOUR_PUBLIC_IP:~/Crypto_Bot/
```

**Server — first install**

```bash
cd ~/Crypto_Bot && bash deploy/scripts/install_server.sh
sudo systemctl start crypto-bot-dashboard
```

**PC — use bot like usual**

```bash
bash deploy/scripts/open_dashboard.sh ubuntu@YOUR_PUBLIC_IP
# Browser → http://localhost:8501 → BOOT BOT ENGINE
```

---

*Related: `VERSIONS.md` (v2.0.1 COMPOUND) · `PATH_B_COMPOUND.md` · `deploy/README.md`*