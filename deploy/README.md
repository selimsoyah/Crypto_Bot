# Deployment (`deploy/`)

| File | Purpose |
| :--- | :--- |
| **[ORACLE_DEPLOY.md](ORACLE_DEPLOY.md)** | **Start here** — full Oracle Always Free step-by-step guide |
| [scripts/sync_to_server.sh](scripts/sync_to_server.sh) | Run on **PC** — rsync project to VM |
| [scripts/install_server.sh](scripts/install_server.sh) | Run on **VM** — Python venv + systemd + firewall |
| [scripts/open_dashboard.sh](scripts/open_dashboard.sh) | Run on **PC** — SSH tunnel to `localhost:8501` |
| [scripts/startup_session.sh](scripts/startup_session.sh) | Run on **PC** — morning setup: local cleanup + health check + tunnel + browser |
| [scripts/stop_session.sh](scripts/stop_session.sh) | Run on **PC** — close background dashboard tunnel |
| [scripts/preflight_shutdown.sh](scripts/preflight_shutdown.sh) | Run on **PC** — verify safe to power off laptop |

Session CSV exports are written on graceful shutdown **and** when the engine crashes (thread `finally`, process exit, or auto-recovery on next dashboard boot).
| [scripts/remote_health_check.sh](scripts/remote_health_check.sh) | Run on **VM** — server-side health checks (also used by preflight) |
| [systemd/crypto-bot-dashboard.service](systemd/crypto-bot-dashboard.service) | Auto-start Streamlit on boot |
| [streamlit/config.toml](streamlit/config.toml) | Bind dashboard to localhost only |

## Quick start

1. Read [ORACLE_DEPLOY.md](ORACLE_DEPLOY.md) Phase 1 — create Oracle VM  
2. PC: `bash deploy/scripts/sync_to_server.sh ubuntu@YOUR_IP`  
3. PC: `scp .env ubuntu@YOUR_IP:~/Crypto_Bot/.env`  
4. VM: `bash deploy/scripts/install_server.sh`  
5. PC: `SSH_KEY=~/oracle-key.key bash deploy/scripts/startup_session.sh ubuntu@YOUR_IP` → boot bot if needed  
