#!/usr/bin/env bash
# Show trades + key log events for the most recent (or specified) bot session.
#
# Run from PC (pipes over SSH):
#   SSH_KEY=~/oracle-key.key bash deploy/scripts/show_last_session.sh ubuntu@79.76.98.67
#   SSH_KEY=~/oracle-key.key bash deploy/scripts/show_last_session.sh ubuntu@79.76.98.67 2593ad7ba1d1
#
# Run on server:
#   cd ~/Crypto_Bot && bash deploy/scripts/show_last_session.sh
#
# Export recovered CSV for a crashed session (needs updated bot_loop.py on server):
#   SSH_KEY=~/oracle-key.key EXPORT_CSV=1 bash deploy/scripts/show_last_session.sh ubuntu@79.76.98.67
set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SESSION_ID="${1:-}"
EXPORT_CSV="${EXPORT_CSV:-0}"
REMOTE_MODE=false

if [[ $# -ge 1 && "$1" == *"@"* ]]; then
  REMOTE_MODE=true
  SERVER="$1"
  SESSION_ID="${2:-}"
  SSH_KEY="${SSH_KEY:-}"
  SSH_EXTRA=()
  if [[ -n "$SSH_KEY" ]]; then
    SSH_EXTRA=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)
  fi
elif [[ $# -ge 1 && "$1" != *"@"* ]]; then
  SESSION_ID="$1"
fi

run_py() {
  BOT_DIR="${BOT_DIR:-$HOME/Crypto_Bot}" \
  SESSION_ID="$SESSION_ID" \
  EXPORT_CSV="$EXPORT_CSV" \
  python3 <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

bot_dir = Path(os.environ.get("BOT_DIR", "."))
db_path = bot_dir / "bot_status_log.db"
session_id = os.environ.get("SESSION_ID", "").strip()
export_csv = os.environ.get("EXPORT_CSV", "0") == "1"

if not db_path.is_file():
    print(f"[ERROR] Database not found: {db_path}", file=sys.stderr)
    raise SystemExit(2)

con = sqlite3.connect(db_path)
cur = con.cursor()

sessions = cur.execute(
    """
    SELECT s.session_id, MIN(s.ts), MAX(s.ts), COUNT(s.id),
           (SELECT COUNT(*) FROM trades t WHERE t.session_id = s.session_id)
    FROM status_log s
    GROUP BY s.session_id
    ORDER BY MAX(s.id) DESC
    LIMIT 15
    """
).fetchall()

print("Recent sessions:")
for sid, start_ts, end_ts, n_rows, n_trades in sessions:
    mark = " <--" if session_id and sid == session_id else ""
    print(f"  {sid}  trades={n_trades}  logs={n_rows}  {start_ts} -> {end_ts}{mark}")

if not session_id:
  if sessions:
    by_trades = cur.execute(
      """
      SELECT session_id FROM trades
      GROUP BY session_id
      ORDER BY COUNT(*) DESC, MAX(id) DESC
      LIMIT 1
      """
    ).fetchone()
    if by_trades:
      session_id = by_trades[0]
    else:
      session_id = sessions[0][0]
  else:
    print("\n[ERROR] No sessions in database.", file=sys.stderr)
    raise SystemExit(1)

meta = cur.execute(
    "SELECT MIN(ts), MAX(ts), COUNT(*) FROM status_log WHERE session_id=?",
    (session_id,),
).fetchone()
if not meta or meta[2] == 0:
    print(f"\n[ERROR] Session not found: {session_id}", file=sys.stderr)
    raise SystemExit(1)

print()
print("=" * 72)
print(f"SESSION: {session_id}")
print(f"  First log: {meta[0]}")
print(f"  Last log:  {meta[1]}")
print(f"  Status rows: {meta[2]}")
print("=" * 72)

trades = cur.execute(
    """
    SELECT exit_ts, side, entry_price, exit_price, quantity, realized_pnl, outcome
    FROM trades WHERE session_id=? ORDER BY id
    """,
    (session_id,),
).fetchall()

print(f"\nCLOSED TRADES ({len(trades)}):")
print(f"{'EXIT (UTC)':<20} {'SIDE':<6} {'ENTRY':>10} {'EXIT':>10} {'QTY':>8} {'PNL':>9} {'OUT'}")
print("-" * 72)
total = 0.0
for exit_ts, side, ep, xp, qty, pnl, outcome in trades:
    total += float(pnl or 0)
    print(
        f"{exit_ts:<20} {side:<6} {ep:>10.2f} {xp:>10.2f} "
        f"{qty:>8.4f} {pnl:>+9.2f} {outcome}"
    )
print("-" * 72)
print(f"Net realized PnL: {total:+.2f} USDT")

print("\nKEY EVENTS (opens, closes, blocks — skipping HOLD heartbeats):")
events = cur.execute(
    """
    SELECT ts, action, open_position, reason FROM status_log
    WHERE session_id=? AND action != 'HOLD'
    ORDER BY id
    """,
    (session_id,),
).fetchall()
if not events:
    events = cur.execute(
        """
        SELECT ts, action, open_position, reason FROM status_log
        WHERE session_id=? ORDER BY id DESC LIMIT 25
        """,
        (session_id,),
    ).fetchall()
    events = list(reversed(events))

for ts, action, pos, reason in events[-50:]:
    r = (reason or "").replace("\n", " ")[:72]
    print(f"  {ts}  {action:<16} {(pos or 'FLAT'):<6}  {r}")

if len(events) > 50:
    print(f"  ... ({len(events) - 50} earlier events omitted)")

con.close()

if export_csv:
    sys.path.insert(0, str(bot_dir))
    try:
        from bot_loop import recover_orphan_session_export
        from trade_store import TradeStore

        report = recover_orphan_session_export(TradeStore())
        if report and report.csv_path:
            print()
            print(f"CSV export: {report.csv_path}")
        else:
            print()
            print("[WARN] CSV export not produced — sync latest code to server first.")
    except Exception as exc:
        print()
        print(f"[WARN] CSV export failed: {exc}")

print()
print("Full status log is in bot_status_log.db (status_log table).")
print("Dashboard Session Activity / Closed Trades show the same data when engine is up.")
PY
}

if [[ "$REMOTE_MODE" == true ]]; then
  echo "==> Remote session report ($SERVER)"
  echo ""
  ssh "${SSH_EXTRA[@]}" "$SERVER" \
    "BOT_DIR=\$HOME/Crypto_Bot SESSION_ID='${SESSION_ID}' EXPORT_CSV='${EXPORT_CSV}' bash -s" \
    < "$LOCAL_DIR/deploy/scripts/show_last_session.sh"
else
  run_py
fi
