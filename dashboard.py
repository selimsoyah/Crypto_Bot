"""
dashboard.py
============
Cinematic Streamlit control room for the BTC/USDT multi-class **Futures** bot.

Upgrades
--------
1. Verbose, explanatory log feed (the "why" behind every decision).
2. Monospace, terminal/hacker-style live log monitor with colour-coded badges.
3. Embedded free, live TradingView Advanced Chart widget.
4. Custom "popping" HTML/CSS metric cards with hover micro-interactions.
5. Redesigned BOOT / SHUTDOWN control console with a pulsing status beacon.

Launch with:

    streamlit run dashboard.py
"""

from __future__ import annotations

import html
import json
import os

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import config
import dashboard_stats
import exchange_client

# --------------------------------------------------------------------------- #
# Page configuration & global theme                                           #
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="BTC/USDT ML Futures Desk",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

_GLOBAL_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap');

    .stApp { background-color: #070A0F; color: #E2E8F0; }
    section[data-testid="stSidebar"] { background-color: #0B0E14; border-right: 1px solid #1E293B; }
    h1, h2, h3 { color: #F8FAFC; letter-spacing: 0.3px; }

    /* ---- Popping metric cards -------------------------------------- */
    .metric-grid { display: flex; gap: 16px; }
    .metric-card {
        flex: 1;
        background: linear-gradient(160deg, #0F1623 0%, #0B0E14 100%);
        border: 1px solid #1E293B;
        border-radius: 16px;
        padding: 20px 22px;
        box-shadow: 0 6px 18px rgba(0,0,0,0.45);
        transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
    }
    .metric-card:hover {
        transform: translateY(-4px) scale(1.015);
        box-shadow: 0 12px 30px rgba(16,185,129,0.18);
        border-color: #334155;
    }
    .metric-label {
        font-size: 0.78rem; font-weight: 600; letter-spacing: 1.2px;
        text-transform: uppercase; color: #64748B; margin-bottom: 10px;
    }
    .metric-value { font-size: 2.0rem; font-weight: 700; color: #F8FAFC; line-height: 1.1; }
    .metric-sub { font-size: 0.85rem; color: #94A3B8; margin-top: 6px; }
    .glow-green { color: #10B981; text-shadow: 0 0 14px rgba(16,185,129,0.55); }
    .glow-red   { color: #EF4444; text-shadow: 0 0 14px rgba(239,68,68,0.55); }

    /* ---- Position pill --------------------------------------------- */
    .pill {
        display: inline-block; padding: 6px 18px; border-radius: 999px;
        font-size: 1.25rem; font-weight: 700; letter-spacing: 1px;
    }
    .pill-long  { background: rgba(16,185,129,0.15); color: #10B981; border: 1px solid #10B981; }
    .pill-short { background: rgba(239,68,68,0.15); color: #EF4444; border: 1px solid #EF4444; }
    .pill-flat  { background: rgba(100,116,139,0.15); color: #94A3B8; border: 1px solid #475569; }

    /* ---- Terminal log monitor -------------------------------------- */
    .terminal {
        background: #0B0E14;
        border: 1px solid #1E293B;
        border-radius: 12px;
        padding: 16px 18px;
        height: 320px;
        overflow-y: scroll;
        font-family: 'JetBrains Mono', 'Fira Code', 'Courier New', monospace;
        font-size: 0.82rem;
        line-height: 1.6;
        color: #E2E8F0;
        scrollbar-color: #334155 #0B0E14;
        scrollbar-width: thin;
        box-shadow: inset 0 0 24px rgba(0,0,0,0.55);
    }
    .terminal::-webkit-scrollbar { width: 10px; }
    .terminal::-webkit-scrollbar-track { background: #0B0E14; }
    .terminal::-webkit-scrollbar-thumb { background: #334155; border-radius: 6px; }
    .term-line { white-space: pre-wrap; word-break: break-word; }
    .term-ts { color: #475569; }

    /* ---- Status beacon --------------------------------------------- */
    .beacon-wrap {
        display: flex; align-items: center; gap: 10px;
        padding: 12px 16px; border-radius: 12px; margin-top: 12px;
        background: #0B0E14; border: 1px solid #1E293B;
        font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 0.9rem;
    }
    .dot { height: 14px; width: 14px; border-radius: 50%; display: inline-block; }
    .dot-live { background: #10B981; box-shadow: 0 0 10px #10B981; animation: pulse 1.5s infinite; }
    .dot-off  { background: #475569; }
    @keyframes pulse {
        0%   { box-shadow: 0 0 0 0 rgba(16,185,129,0.7); }
        70%  { box-shadow: 0 0 0 12px rgba(16,185,129,0); }
        100% { box-shadow: 0 0 0 0 rgba(16,185,129,0); }
    }

    /* ---- Venue banner (Phase 3) ------------------------------------ */
    .venue-banner {
        padding: 14px 20px; border-radius: 12px; margin-bottom: 16px;
        font-weight: 800; letter-spacing: 1.5px; text-align: center;
        font-size: 1.05rem; text-transform: uppercase;
    }
    .venue-testnet {
        background: rgba(16,185,129,0.12); color: #10B981;
        border: 2px solid #10B981; box-shadow: 0 0 20px rgba(16,185,129,0.25);
    }
    .venue-live {
        background: rgba(239,68,68,0.18); color: #FCA5A5;
        border: 2px solid #EF4444; box-shadow: 0 0 24px rgba(239,68,68,0.35);
        animation: pulse-red 2s infinite;
    }
    @keyframes pulse-red {
        0%   { box-shadow: 0 0 0 0 rgba(239,68,68,0.5); }
        70%  { box-shadow: 0 0 0 10px rgba(239,68,68,0); }
        100% { box-shadow: 0 0 0 0 rgba(239,68,68,0); }
    }

    /* ---- Control buttons ------------------------------------------- */
    div[data-testid="column"] .stButton > button {
        width: 100%; border-radius: 12px; font-weight: 700; letter-spacing: 0.6px;
        padding: 12px 0; border: 1px solid #1E293B; transition: all 0.15s ease;
    }
    .stProgress > div > div > div > div { background-color: #10B981; }

    /* ---- Live position tracker ------------------------------------- */
    .pos-panel {
        background: linear-gradient(160deg, #0F1623 0%, #0B0E14 100%);
        border: 1px solid #1E293B;
        border-radius: 16px;
        padding: 22px 24px;
        margin-bottom: 8px;
        box-shadow: 0 6px 18px rgba(0,0,0,0.45);
    }
    .pos-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
    .pos-cell {
        background: #0B0E14;
        border: 1px solid #1E293B;
        border-radius: 12px;
        padding: 14px 16px;
    }
    .pos-cell-label {
        font-size: 0.72rem; font-weight: 600; letter-spacing: 1px;
        text-transform: uppercase; color: #64748B; margin-bottom: 8px;
    }
    .pos-cell-value {
        font-size: 1.35rem; font-weight: 700; color: #F8FAFC;
        font-family: 'JetBrains Mono', monospace;
    }
    .pnl-banner {
        margin-top: 14px; padding: 14px 18px; border-radius: 12px;
        font-size: 1.15rem; font-weight: 700;
        font-family: 'JetBrains Mono', monospace;
        border: 1px solid #1E293B;
    }
    .pnl-profit { background: rgba(16,185,129,0.12); color: #10B981; }
    .pnl-loss   { background: rgba(239,68,68,0.12); color: #EF4444; }
    .audit-grid {
        display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
        margin-top: 14px;
    }
    .audit-cell {
        background: #0B0E14; border: 1px dashed #334155; border-radius: 10px;
        padding: 12px 14px;
    }
    .audit-label { font-size: 0.72rem; color: #64748B; text-transform: uppercase; }
    .audit-strip {
        display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px;
        margin: 12px 0 18px; padding: 14px 16px; border-radius: 12px;
        background: rgba(15, 23, 42, 0.6); border: 1px solid rgba(100, 116, 139, 0.25);
    }
    .audit-value { font-size: 1rem; color: #E2E8F0; font-weight: 600; margin-top: 4px; }
    .pos-idle {
        text-align: center; padding: 28px 16px; color: #94A3B8;
        font-size: 1rem; border: 1px dashed #334155; border-radius: 12px;
        background: #0B0E14;
    }

    /* ---- Live PnL metric strip ------------------------------------- */
    .pnl-live-card {
        background: linear-gradient(160deg, #0F1623 0%, #0B0E14 100%);
        border: 1px solid #1E293B;
        border-radius: 16px;
        padding: 20px 22px;
        box-shadow: 0 6px 18px rgba(0,0,0,0.45);
        transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
        min-height: 118px;
    }
    .pnl-live-card:hover {
        transform: translateY(-3px);
        box-shadow: 0 10px 26px rgba(16,185,129,0.14);
        border-color: #334155;
    }
    .pnl-live-label {
        font-size: 0.78rem; font-weight: 600; letter-spacing: 1.1px;
        text-transform: uppercase; color: #64748B; margin-bottom: 10px;
    }
    .pnl-live-value {
        font-size: 1.85rem; font-weight: 700; line-height: 1.15;
        font-family: 'JetBrains Mono', monospace;
    }
    .pnl-live-sub {
        font-size: 0.82rem; color: #94A3B8; margin-top: 8px;
    }
    .pnl-neutral { color: #64748B; }

    /* ---- Retro TRADE LOG (screenshot style) ------------------------ */
    .trade-log-wrap {
        background: #050805;
        border: 1px solid #1a3d1a;
        border-radius: 4px;
        padding: 18px 20px 14px;
        box-shadow: inset 0 0 40px rgba(0, 40, 0, 0.35);
        font-family: 'JetBrains Mono', 'Courier New', monospace;
    }
    .trade-log-title {
        color: #39ff14;
        font-size: 1.05rem;
        font-weight: 700;
        letter-spacing: 2px;
        text-transform: uppercase;
        text-shadow: 0 0 8px rgba(57, 255, 20, 0.6);
        margin-bottom: 10px;
    }
    .trade-log-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.88rem;
        color: #39ff14;
        text-shadow: 0 0 6px rgba(57, 255, 20, 0.35);
    }
    .trade-log-table thead tr {
        border-bottom: 1px solid #39ff14;
    }
    .trade-log-table th {
        text-align: left;
        padding: 8px 12px 10px 0;
        font-weight: 700;
        letter-spacing: 1px;
        color: #39ff14;
    }
    .trade-log-table td {
        padding: 7px 12px 7px 0;
        white-space: nowrap;
    }
    .trade-log-table tbody tr:hover {
        background: rgba(57, 255, 20, 0.04);
    }
    .tl-win { color: #39ff14 !important; text-shadow: 0 0 8px rgba(57, 255, 20, 0.55); }
    .tl-loss { color: #ff3131 !important; text-shadow: 0 0 8px rgba(255, 49, 49, 0.55); }
    .tl-muted { color: #2d8a2d; }
    .tl-empty {
        color: #2d8a2d;
        padding: 24px 0;
        text-align: center;
        font-size: 0.9rem;
    }

    /* ---- Essential metrics strip ----------------------------------- */
    .ess-grid {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 12px;
        margin-bottom: 18px;
    }
    .ess-card {
        background: #050805;
        border: 1px solid #1a3d1a;
        border-radius: 4px;
        padding: 14px 16px;
        font-family: 'JetBrains Mono', monospace;
    }
    .ess-label {
        font-size: 0.68rem;
        letter-spacing: 1.4px;
        text-transform: uppercase;
        color: #2d8a2d;
        margin-bottom: 6px;
    }
    .ess-value {
        font-size: 1.35rem;
        font-weight: 700;
        color: #39ff14;
        text-shadow: 0 0 8px rgba(57, 255, 20, 0.45);
    }
    .ess-value.pos { color: #39ff14; }
    .ess-value.neg { color: #ff3131; text-shadow: 0 0 8px rgba(255, 49, 49, 0.45); }
    .ess-sub { font-size: 0.75rem; color: #2d8a2d; margin-top: 4px; }

    /* ---- Bot heartbeat strip --------------------------------------- */
    .hb-strip {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 14px 22px;
        background: #050805;
        border: 1px solid #1a3d1a;
        border-radius: 4px;
        padding: 12px 18px;
        margin-bottom: 14px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.82rem;
    }
    .hb-live  { border-color: #39ff14; box-shadow: 0 0 12px rgba(57, 255, 20, 0.15); }
    .hb-warn  { border-color: #fbbf24; box-shadow: 0 0 12px rgba(251, 191, 36, 0.12); }
    .hb-off   { border-color: #334155; }
    .hb-dot {
        width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0;
    }
    .hb-dot-live {
        background: #39ff14;
        box-shadow: 0 0 10px #39ff14;
        animation: hb-pulse 1.4s infinite;
    }
    .hb-dot-warn { background: #fbbf24; box-shadow: 0 0 8px #fbbf24; }
    .hb-dot-off  { background: #475569; }
    @keyframes hb-pulse {
        0%   { box-shadow: 0 0 0 0 rgba(57, 255, 20, 0.65); }
        70%  { box-shadow: 0 0 0 8px rgba(57, 255, 20, 0); }
        100% { box-shadow: 0 0 0 0 rgba(57, 255, 20, 0); }
    }
    .hb-status { font-weight: 800; letter-spacing: 1.5px; color: #39ff14; }
    .hb-status-warn { color: #fbbf24; }
    .hb-status-off  { color: #64748B; }
    .hb-meta { color: #2d8a2d; }
    .hb-detail { color: #39ff14; flex: 1 1 100%; font-size: 0.78rem; opacity: 0.9; }
</style>
"""
st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# Auto refresh                                                                #
# --------------------------------------------------------------------------- #
REFRESH_MS = 5000
try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(interval=REFRESH_MS, key="auto_refresh")
    _AUTOREFRESH = True
except Exception:
    _AUTOREFRESH = False


# --------------------------------------------------------------------------- #
# Data loading helpers (SQLite store is the single source of truth)           #
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def get_store():
    """Process-wide TradeStore handle (SQLite, WAL — safe for concurrent reads)."""
    from trade_store import TradeStore

    return TradeStore()


def get_live_thresholds() -> tuple[float, float, str]:
    """Thresholds the bot ACTUALLY trades with (same resolver as bot_loop)."""
    from bot_loop import resolve_live_thresholds

    return resolve_live_thresholds()


def load_log(limit: int = 3000) -> pd.DataFrame:
    """Load recent status rows from the SQLite store (chronological)."""
    try:
        return get_store().read_status_df(limit=limit)
    except Exception as exc:
        st.sidebar.error(f"Could not read status log: {exc}")
        return pd.DataFrame(columns=config.LOG_COLUMNS)


def load_trades() -> pd.DataFrame:
    """Load the ground-truth completed-trades ledger."""
    try:
        return get_store().read_trades_df()
    except Exception as exc:
        st.sidebar.error(f"Could not read trades ledger: {exc}")
        return pd.DataFrame()


compute_stats = dashboard_stats.compute_stats
compute_closed_trade_stats = dashboard_stats.compute_closed_trade_stats
get_live_position_pnl = dashboard_stats.get_live_position_pnl
reconcile_floating_pnl = dashboard_stats.reconcile_floating_pnl
compute_session_risk_pnl = dashboard_stats.compute_session_risk_pnl
allocation_label_from_risk = dashboard_stats.allocation_label_from_risk


def render_bot_heartbeat(bot, log: pd.DataFrame) -> None:
    """Main-page engine heartbeat — confirms the bot is scanning and healthy."""
    h = dashboard_stats.bot_health(bot, log)
    strip_cls = {"LIVE": "hb-live", "BOOTING": "hb-live", "DEGRADED": "hb-warn", "STALE": "hb-warn", "OFFLINE": "hb-off"}.get(
        h["status"], "hb-off"
    )
    dot_cls = {
        "LIVE": "hb-dot-live",
        "BOOTING": "hb-dot-live",
        "DEGRADED": "hb-dot-warn",
        "STALE": "hb-dot-warn",
        "OFFLINE": "hb-dot-off",
    }.get(h["status"], "hb-dot-off")
    status_cls = {
        "LIVE": "hb-status",
        "BOOTING": "hb-status",
        "DEGRADED": "hb-status-warn",
        "STALE": "hb-status-warn",
        "OFFLINE": "hb-status-off",
    }.get(h["status"], "hb-status-off")

    age = f"{int(h['seconds_ago'])}s ago" if h["seconds_ago"] is not None else "—"
    st.markdown(
        f'<div class="hb-strip {strip_cls}">'
        f'<span class="hb-dot {dot_cls}"></span>'
        f'<span class="{status_cls}">ENGINE {html.escape(h["status"])}</span>'
        f'<span class="hb-meta">Last scan: {html.escape(h["last_scan"])} ({age})</span>'
        f'<span class="hb-meta">Action: {html.escape(h["last_action"])}</span>'
        f'<span class="hb-meta">Position: {html.escape(h["open_position"])}</span>'
        f'<span class="hb-detail">{html.escape(h["detail"])}</span>'
        f"</div>",
        unsafe_allow_html=True,
    )


def render_essential_metrics(
    trades: pd.DataFrame,
    log: pd.DataFrame,
    exchange_pos: dict | None = None,
    bot=None,
) -> None:
    """Compact headline strip — wallet, net PnL, open PnL, win rate."""
    m = dashboard_stats.essential_metrics(trades, log, exchange_pos, bot=bot)

    def _cls(val: float) -> str:
        if val > 0:
            return "pos"
        if val < 0:
            return "neg"
        return ""

    net_cls = _cls(m["net_pnl"])
    open_cls = _cls(m["open_pnl"])
    net_sign = "+" if m["net_pnl"] >= 0 else ""
    open_sign = "+" if m["open_pnl"] >= 0 else ""

    position_label = m["open_side"] if m["open_side"] != "FLAT" else "FLAT"
    wr_cls = "pos" if m["win_rate"] >= 50 else ("neg" if m["total_trades"] else "")

    html_block = f"""
    <div class="ess-grid">
        <div class="ess-card">
            <div class="ess-label">Wallet</div>
            <div class="ess-value">${m['wallet_balance']:,.2f}</div>
            <div class="ess-sub">USDT margin</div>
        </div>
        <div class="ess-card">
            <div class="ess-label">Net PnL</div>
            <div class="ess-value {net_cls}">{net_sign}{m['net_pnl']:.2f}</div>
            <div class="ess-sub">{m['total_trades']} closed</div>
        </div>
        <div class="ess-card">
            <div class="ess-label">Open PnL</div>
            <div class="ess-value {open_cls}">{open_sign}{m['open_pnl']:.2f}</div>
            <div class="ess-sub">{position_label}</div>
        </div>
        <div class="ess-card">
            <div class="ess-label">Win Rate</div>
            <div class="ess-value {wr_cls}">{m['win_rate']:.1f}%</div>
            <div class="ess-sub">{m['wins']}W / {m['losses']}L · {html.escape(m['direction'])}</div>
        </div>
    </div>
    """
    st.markdown(html_block, unsafe_allow_html=True)


def render_compound_strip(trades: pd.DataFrame, log: pd.DataFrame, bot=None) -> None:
    """Path B compounding metrics — weekly expectancy and sizing multiplier."""
    if not config.is_compound_profile():
        return

    m = dashboard_stats.essential_metrics(trades, log, bot=bot)
    long_thr, short_thr, _ = get_live_thresholds()
    prob_long = float(log.iloc[-1].get("Prob_Long", 0.0) or 0.0) if not log.empty else 0.0
    prob_short = float(log.iloc[-1].get("Prob_Short", 0.0) or 0.0) if not log.empty else 0.0
    dist = dashboard_stats.threshold_distance(prob_long, prob_short, long_thr, short_thr)

    pnl_cls = "pos" if m["pnl_7d"] >= 0 else "neg"
    exp_cls = "pos" if m["expectancy"] >= 0 else "neg"
    pnl_sign = "+" if m["pnl_7d"] >= 0 else ""
    exp_sign = "+" if m["expectancy"] >= 0 else ""

    st.markdown(
        f'<div class="audit-strip">'
        f'<div><div class="audit-label">Profile</div>'
        f'<div class="audit-value">{html.escape(config.profile_summary())}</div></div>'
        f'<div><div class="audit-label">7d PnL</div>'
        f'<div class="audit-value {pnl_cls}">{pnl_sign}{m["pnl_7d"]:.2f}</div></div>'
        f'<div><div class="audit-label">7d Trades</div>'
        f'<div class="audit-value">{m["trades_7d"]}</div></div>'
        f'<div><div class="audit-label">Expectancy</div>'
        f'<div class="audit-value {exp_cls}">{exp_sign}{m["expectancy"]:.2f}</div></div>'
        f'<div><div class="audit-label">Size mult</div>'
        f'<div class="audit-value">{m["size_mult"]:.2f}x</div></div>'
        f'<div><div class="audit-label">Signal gap</div>'
        f'<div class="audit-value">L {dist["long_gap"]*100:.1f}% · S {dist["short_gap"]*100:.1f}%</div></div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def render_trade_log(trades: pd.DataFrame, max_rows: int = 50) -> None:
    """Retro terminal trade log table — green wins, red losses."""
    rows = dashboard_stats.trades_to_log_rows(trades)
    if max_rows:
        rows = rows[-max_rows:]

    if not rows:
        body = '<div class="tl-empty">No closed trades yet — boot the engine to begin.</div>'
        table = ""
    else:
        table_rows = []
        for r in rows:
            cls = "tl-win" if r["won"] else "tl-loss"
            pnl_sign = "+" if r["pnl"] >= 0 else ""
            table_rows.append(
                f"<tr>"
                f'<td class="tl-muted">{html.escape(r["time"])}</td>'
                f'<td class="{cls}">{html.escape(r["side"])}</td>'
                f'<td class="tl-muted">{r["entry"]:,.2f}</td>'
                f'<td class="tl-muted">{r["exit"]:,.2f}</td>'
                f'<td class="tl-muted">{r["sh"]}</td>'
                f'<td class="{cls}">{html.escape(r["status"])}</td>'
                f'<td class="{cls}">{pnl_sign}{r["pnl"]:.2f}</td>'
                f"</tr>"
            )
        table = (
            '<table class="trade-log-table">'
            "<thead><tr>"
            "<th>TIME</th><th>SIDE</th><th>ENTRY</th><th>EXIT</th>"
            "<th>SH</th><th>STATUS</th><th>PNL</th>"
            "</tr></thead><tbody>"
            + "".join(table_rows)
            + "</tbody></table>"
        )
        body = table

    st.markdown(
        f'<div class="trade-log-wrap">'
        f'<div class="trade-log-title">Trade Log</div>{body}</div>',
        unsafe_allow_html=True,
    )


@st.cache_resource(show_spinner=False)
def _futures_client():
    """Return a cached execution client for read-only dashboard queries."""
    return exchange_client.build_execution_client()


@st.cache_data(ttl=max(1, REFRESH_MS // 1000), show_spinner=False)
def fetch_live_position(symbol: str = config.SYMBOL) -> dict:
    """Cached wrapper around ``dashboard_stats.fetch_live_position``."""
    return dashboard_stats.fetch_live_position(symbol=symbol, client=_futures_client())


def _bracket_prices(entry: float, side: str, bot=None) -> tuple[float, float]:
    """Return (take_profit, stop_loss) from the live bot state or shared math."""
    if bot is not None and bot.state.position is not None:
        pos = bot.state.position
        if pos.entry_price > 0:
            return pos.take_profit_price, pos.stop_loss_price
    # Same function the bot uses at fill time — single source of truth.
    from bot_loop import bracket_prices

    return bracket_prices(side, entry)


def render_position_tracker(bot=None) -> None:
    """Render the live open-position panel with PnL and order audit details."""
    with st.container():
        st.subheader("📊 Live Position Tracker")
        pos = fetch_live_position()

        if pos.get("status") == "error":
            st.warning(
                f"⚠️ Exchange position query FAILED — position state UNKNOWN "
                f"(not flat). {pos.get('message', '')}"
            )
            if bot is not None and bot.state.position is not None:
                mem = bot.state.position
                st.info(
                    f"Bot memory still holds an open **{mem.side}** @ "
                    f"${mem.entry_price:,.2f} (last known internal state)."
                )
            return

        if pos.get("status") != "ok":
            long_thr, short_thr, _ = get_live_thresholds()
            edge = min(long_thr, short_thr) * 100.0
            st.markdown(
                '<div class="pos-panel"><div class="pos-idle">'
                "✨ No open trades active. Bot is monitoring the market for a "
                f"{edge:.0f}% probability edge..."
                "</div></div>",
                unsafe_allow_html=True,
            )
            return

        tp, sl = _bracket_prices(pos["entry_price"], pos["side"], bot)
        pnl = pos["unrealized_pnl"]
        pct = pos["pct_change"]
        pnl_class = "pnl-profit" if pnl >= 0 else "pnl-loss"
        pnl_icon = "🟩" if pnl >= 0 else "🟥"
        pnl_word = "Profit" if pnl >= 0 else "Loss"
        pnl_sign = "+" if pnl >= 0 else ""

        grid = f"""
        <div class="pos-panel">
            <div class="pos-grid">
                <div class="pos-cell">
                    <div class="pos-cell-label">Entry Price</div>
                    <div class="pos-cell-value">${pos['entry_price']:,.2f}</div>
                </div>
                <div class="pos-cell">
                    <div class="pos-cell-label">Current Market Price</div>
                    <div class="pos-cell-value">${pos['mark_price']:,.2f}</div>
                </div>
                <div class="pos-cell">
                    <div class="pos-cell-label">Take Profit (TP)</div>
                    <div class="pos-cell-value" style="color:#10B981;">${tp:,.2f}</div>
                </div>
                <div class="pos-cell">
                    <div class="pos-cell-label">Stop Loss (SL)</div>
                    <div class="pos-cell-value" style="color:#EF4444;">${sl:,.2f}</div>
                </div>
            </div>
            <div class="pnl-banner {pnl_class}">
                {pnl_icon} {pnl_word}: {pnl_sign}${abs(pnl):,.2f} ({pnl_sign}{pct:.2f}%)
                &nbsp;·&nbsp; Side: <strong>{pos['side']}</strong>
                &nbsp;·&nbsp; Qty: {pos['quantity']:.4f} BTC
            </div>
            <div class="audit-grid">
                <div class="audit-cell">
                    <div class="audit-label">Leverage Setting</div>
                    <div class="audit-value">{pos['leverage']}x</div>
                </div>
                <div class="audit-cell">
                    <div class="audit-label">Total Notional Size</div>
                    <div class="audit-value">${pos['notional']:,.2f} USDT</div>
                </div>
                <div class="audit-cell">
                    <div class="audit-label">Required Margin</div>
                    <div class="audit-value">${pos['margin']:,.2f} USDT</div>
                </div>
            </div>
        </div>
        """
        st.markdown(grid, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# 4. Popping metric cards                                                     #
# --------------------------------------------------------------------------- #
def render_metric_cards(stats: dict) -> None:
    """Render four custom HTML/CSS metric cards side by side."""
    direction = stats["open_status"].upper()
    if direction == "LONG":
        pill = '<span class="pill pill-long">LONG</span>'
    elif direction == "SHORT":
        pill = '<span class="pill pill-short">SHORT</span>'
    else:
        pill = '<span class="pill pill-flat">FLAT</span>'

    realized = stats["realized_pnl"]
    unrealized = stats["unrealized_pnl"]
    realized_class = "glow-green" if realized >= 0 else "glow-red"
    unrealized_class = "glow-green" if unrealized > 0 else ("glow-red" if unrealized < 0 else "")
    unrealized_sign = "+" if unrealized > 0 else ""
    realized_sign = "+" if realized >= 0 else ""
    win_class = "glow-green" if stats["win_rate"] >= 50 else "glow-red"

    cards = f"""
    <div class="metric-grid">
        <div class="metric-card">
            <div class="metric-label">USDT Futures Balance</div>
            <div class="metric-value">${stats['balance']:,.2f}</div>
            <div class="metric-sub">{stats.get('allocation_label', f'{config.CASH_ALLOCATION_PCT:.0%} base · {config.LEVERAGE}x')}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Active Position</div>
            <div class="metric-value">{pill}</div>
            <div class="metric-sub"><span class="{unrealized_class}">Unrealized {unrealized_sign}{unrealized:,.2f} USDT</span></div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Completed Trades</div>
            <div class="metric-value">{stats['total_trades']}</div>
            <div class="metric-sub">{stats['long_trades']} long · {stats['short_trades']} short</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Win Rate / Net Profit</div>
            <div class="metric-value"><span class="{win_class}">{stats['win_rate']:.1f}%</span></div>
            <div class="metric-sub"><span class="{realized_class}">{realized_sign}{realized:,.2f} USDT net</span> · TP or profitable close</div>
        </div>
    </div>
    """
    st.markdown(cards, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# 3. TradingView Advanced Chart widget                                        #
# --------------------------------------------------------------------------- #
def render_tradingview(symbol: str = "BINANCE:BTCUSDT.P", height: int = 560) -> None:
    """Embed the free, live-updating TradingView Advanced Chart widget."""
    widget = f"""
    <div class="tradingview-widget-container" style="height:{height}px;width:100%">
      <div id="tv_chart" style="height:{height}px;width:100%"></div>
      <script type="text/javascript"
              src="https://s3.tradingview.com/tv.js"></script>
      <script type="text/javascript">
        new TradingView.widget({{
          "autosize": true,
          "symbol": "{symbol}",
          "interval": "60",
          "timezone": "Etc/UTC",
          "theme": "dark",
          "style": "1",
          "locale": "en",
          "toolbar_bg": "#0B0E14",
          "enable_publishing": false,
          "withdateranges": true,
          "hide_side_toolbar": false,
          "allow_symbol_change": false,
          "show_popup_button": true,
          "popup_width": "1000",
          "popup_height": "650",
          "details": true,
          "container_id": "tv_chart"
        }});
      </script>
    </div>
    """
    components.html(widget, height=height + 10)


# --------------------------------------------------------------------------- #
# 2. Terminal-style log monitor                                              #
# --------------------------------------------------------------------------- #
_EVENT_COLORS = {
    "BUY_LONG": "#10B981",
    "FILL_SUCCESS": "#10B981",
    "SHORT_ORDER": "#EF4444",
    "STOP_LOSS": "#EF4444",
    "BLOCKED_LONG": "#FBBF24",
    "BLOCKED_SHORT": "#FBBF24",
    "WARNING": "#FBBF24",
    "STARTUP_FAILED": "#FBBF24",
    "CASH": "#64748B",
    "WAIT": "#E2E8F0",
}
_EVENT_LABELS = {
    "BUY_LONG": "[BUY LONG]",
    "FILL_SUCCESS": "[FILL SUCCESS]",
    "SHORT_ORDER": "[SHORT ORDER]",
    "STOP_LOSS": "[STOP LOSS TRIGGERED]",
    "BLOCKED_LONG": "[WARNING]",
    "BLOCKED_SHORT": "[WARNING]",
    "WARNING": "[WARNING]",
    "STARTUP_FAILED": "[WARNING]",
    "CASH": "[CASH]",
    "WAIT": "[WAIT]",
}


def render_terminal(log: pd.DataFrame, max_lines: int = 200) -> None:
    """Render the colour-coded, monospaced terminal-style log console."""
    if log.empty:
        body = (
            '<div class="term-line"><span style="color:#64748B;">[WAIT]</span> '
            "No log entries yet. Boot the engine to begin streaming decisions."
            "</div>"
        )
    else:
        recent = log.tail(max_lines)
        lines: list[str] = []
        for _, row in recent.iterrows():
            event = str(row.get("Event", "WAIT")) or "WAIT"
            color = _EVENT_COLORS.get(event, "#E2E8F0")
            label = _EVENT_LABELS.get(event, f"[{event}]")
            ts = str(row.get("Timestamp", ""))
            reason = str(row.get("Reason", "") or row.get("Action", ""))
            price = row.get("Current_Price", "")
            try:
                price_str = f"${float(price):,.2f}"
            except (TypeError, ValueError):
                price_str = ""
            # Reasons are plain text in the store; escape them and colour the
            # line by event category (no raw HTML injection from data).
            lines.append(
                f'<div class="term-line">'
                f'<span class="term-ts">{html.escape(ts)}</span> '
                f'<span style="color:{color};font-weight:700;">{label}</span> '
                f'<span style="color:#64748B;">{price_str}</span> '
                f'<span style="color:{color};">{html.escape(reason)}</span>'
                f"</div>"
            )
        # Newest at the bottom (classic terminal); auto-scroll handled by JS.
        body = "\n".join(lines)

    terminal_html = f"""
    <div class="terminal" id="term-box">{body}</div>
    <script>
        var box = window.parent.document.getElementById("term-box")
                  || document.getElementById("term-box");
        if (box) {{ box.scrollTop = box.scrollHeight; }}
    </script>
    """
    st.markdown(terminal_html, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Session PDF export (dashboard shutdown)                                     #
# --------------------------------------------------------------------------- #
def _auto_download_pdf(pdf_bytes: bytes, filename: str) -> None:
    """Trigger a browser download for the session summary PDF."""
    import base64

    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    components.html(
        f"""
        <script>
        (function() {{
            const b64 = {json.dumps(b64)};
            const filename = {json.dumps(filename)};
            const raw = atob(b64);
            const arr = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
            const blob = new Blob([arr], {{type: 'application/pdf'}});
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }})();
        </script>
        """,
        height=0,
    )


def _handle_shutdown_with_pdf(bot) -> None:
    """Stop the bot, build the session dossier, and queue an automatic PDF download."""
    from bot_loop import render_session_report_pdf

    report = bot.stop()
    if report is None:
        st.warning("Bot stopped, but no active session was found to report. Boot the engine first.")
        return

    pdf_bytes = render_session_report_pdf(report)
    ts = report.summary["shutdown_ts"].strftime("%Y-%m-%d_%H%M%S")
    filename = f"session_summary_{ts}.pdf"
    st.session_state["pending_session_pdf"] = (pdf_bytes, filename)
    csv_path = getattr(report, "csv_path", None)
    csv_name = os.path.basename(csv_path) if csv_path else None
    st.session_state["shutdown_notice"] = (filename, csv_name)
    # Drop cached bot so the next boot loads fresh module state after code updates.
    _bot_singleton.clear()


def _show_shutdown_notice() -> None:
    """Display a one-time sidebar notice after the PDF auto-download fires."""
    notice = st.session_state.pop("shutdown_notice", None)
    if notice:
        if isinstance(notice, tuple):
            pdf_name, csv_name = notice
            csv_line = f" CSV: **{csv_name}** (`session_exports/`)." if csv_name else ""
            st.sidebar.success(
                f"Session ended. PDF: **{pdf_name}**.{csv_line} "
                f"Markdown: `session_summary_report.md`"
            )
        else:
            st.sidebar.success(
                f"Session ended. PDF downloaded: **{notice}** "
                f"(markdown copy: `session_summary_report.md`)"
            )


# --------------------------------------------------------------------------- #
# Sidebar: control console                                                    #
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def _bot_singleton() -> tuple:
    """Process-wide TradingBot singleton.

    IMPORTANT: this is deliberately ``st.cache_resource`` (shared across ALL
    browser sessions/tabs) and not ``st.session_state`` (per-tab). Two tabs
    must never each spawn their own trading engine against the same account —
    that exact failure corrupted the old CSV log with interleaved positions.
    """
    try:
        from bot_loop import TradingBot

        return TradingBot(), ""
    except Exception as exc:
        return None, str(exc)


def get_bot():
    """Return the shared TradingBot (or None with the error stored)."""
    bot, err = _bot_singleton()
    if err:
        st.session_state.bot_error = err
    return bot


if st.session_state.get("pending_session_pdf"):
    _pdf_bytes, _pdf_name = st.session_state.pop("pending_session_pdf")
    _auto_download_pdf(_pdf_bytes, _pdf_name)

def _render_venue_banner() -> None:
    """Full-width TESTNET / LIVE banner — impossible to miss."""
    if config.execution_is_live():
        css_class = "venue-live"
        label = "⚠️ LIVE MAINNET EXECUTION — REAL CAPITAL AT RISK"
    else:
        css_class = "venue-testnet"
        label = "✅ TESTNET EXECUTION — NO REAL FUNDS"
    data_src = "Mainnet klines" if config.USE_MAINNET_DATA else "Testnet klines"
    st.markdown(
        f'<div class="venue-banner {css_class}">{label}<br>'
        f'<span style="font-size:0.85rem;font-weight:600;letter-spacing:0.5px;">'
        f"{config.execution_banner_text()} · Data feed: {data_src}"
        f"</span></div>",
        unsafe_allow_html=True,
    )


_show_shutdown_notice()
_render_venue_banner()

with st.sidebar:
    st.markdown("## ⚡ Control Console")
    st.caption(config.execution_banner_text())
    st.caption(
        f"{config.SYMBOL} · {config.INTERVAL} · "
        f"{config.LEVERAGE}x · {config.CASH_ALLOCATION_PCT:.0%} alloc · "
        f"TP +{config.TAKE_PROFIT_PCT:.1%} / SL -{config.STOP_LOSS_PCT:.1%}"
    )

    bot = get_bot()
    creds_ok = config.credentials_present()
    if not creds_ok:
        st.warning("API credentials are placeholders. Set them in `.env` to trade.")

    if config.execution_is_live():
        st.error(
            "EXECUTION_VENUE=LIVE — orders route to **mainnet**. "
            "Type LIVE below to confirm boot."
        )
        live_confirm = st.text_input(
            "Confirm LIVE execution",
            placeholder="Type LIVE",
            key="live_confirm_input",
        )
    else:
        live_confirm = "TESTNET"

    boot_col, kill_col = st.columns(2)
    with boot_col:
        boot = st.button("🚀 BOOT BOT ENGINE", use_container_width=True, type="primary")
    with kill_col:
        kill = st.button("🛑 FORCE SHUTDOWN", use_container_width=True)

    if boot:
        if config.execution_is_live() and live_confirm.strip().upper() != "LIVE":
            st.error("Refused to boot: type LIVE to confirm mainnet execution.")
        elif bot is not None:
            if bot.start():
                st.success("Engine boot sequence initiated.")
            else:
                st.error(
                    bot.state.last_error
                    or "Refused to boot — check instance lock, kill switch, or config."
                )
        else:
            st.error(st.session_state.get("bot_error", "Bot unavailable."))
    if kill:
        if bot is not None:
            _handle_shutdown_with_pdf(bot)
            st.rerun()
        else:
            st.error("Bot unavailable.")

    running = bool(bot and bot.state.running)
    if running:
        beacon = (
            '<div class="beacon-wrap"><span class="dot dot-live"></span>'
            '<span style="color:#10B981;">ENGINE STATUS: RUNNING</span></div>'
        )
    else:
        beacon = (
            '<div class="beacon-wrap"><span class="dot dot-off"></span>'
            '<span style="color:#94A3B8;">ENGINE STATUS: OFFLINE</span></div>'
        )
    st.markdown(beacon, unsafe_allow_html=True)

    if bot and bot.state.connection_degraded:
        st.warning(f"API DEGRADED: {bot.state.connection_error or 'recent failures'}")

    if bot and bot.state.last_error:
        st.caption(f"Last error: {bot.state.last_error}")

    st.divider()
    st.markdown("### 🛡 Risk Controls")
    st.caption(
        f"Max session loss: {config.RISK_MAX_DAILY_LOSS_PCT:.1%} · "
        f"Max consecutive losses: {config.RISK_MAX_CONSECUTIVE_LOSSES} · "
        f"Live leverage recommendation: {config.RISK_RECOMMENDED_LIVE_LEVERAGE}x "
        f"(current config: {config.LEVERAGE}x)"
    )

    risk_snap = bot.risk.snapshot() if bot else None
    kill_file_active = (
        bot.risk.kill_switch_file_active()
        if bot
        else __import__("os").path.exists(config.KILL_SWITCH_FILE)
    )

    if kill_file_active:
        st.error(f"Kill switch ACTIVE ({config.KILL_SWITCH_FILE})")
    elif risk_snap and risk_snap.halted:
        st.warning(risk_snap.halt_reason or "Risk engine halted.")
    elif risk_snap and risk_snap.manual_resume_required:
        st.warning("Consecutive-loss pause — manual resume required.")
    else:
        st.success("Risk gates: OK")

    if risk_snap:
        if bot is not None:
            pnl_pct, realized, unrealized = compute_session_risk_pnl(bot)
            alloc = allocation_label_from_risk(risk_snap)
        else:
            pnl_pct, realized, unrealized = 0.0, 0.0, 0.0
            alloc = (
                f"{config.CASH_ALLOCATION_PCT:.0%} base · vol-scaled · "
                f"{config.LEVERAGE}x"
            )
        st.caption(
            f"Session equity start: ${risk_snap.session_start_equity:,.2f} · "
            f"PnL ${realized + unrealized:+,.2f} "
            f"(real ${realized:,.2f} + unreal ${unrealized:,.2f}, {pnl_pct * 100:+.2f}%) · "
            f"Consecutive losses: {risk_snap.consecutive_losses} · "
            f"{alloc}"
        )

    ks_col, clr_col = st.columns(2)
    with ks_col:
        engage_kill = st.button("☠️ KILL SWITCH", use_container_width=True)
    with clr_col:
        clear_kill = st.button("Clear Kill Switch", use_container_width=True)

    if engage_kill:
        if bot is not None and bot.state.running:
            bot.activate_kill_switch("Dashboard kill switch engaged")
            _handle_shutdown_with_pdf(bot)
            st.rerun()
        else:
            from risk_engine import RiskEngine

            RiskEngine().trigger_kill_switch("Dashboard kill switch (engine offline)")
            st.error("Kill switch file written. Clear it before next boot.")
            st.rerun()

    if clear_kill:
        if bot is not None:
            bot.clear_kill_switch()
        else:
            from risk_engine import RiskEngine

            RiskEngine().clear_kill_switch()
        st.success("Kill switch cleared.")
        st.rerun()

    if risk_snap and risk_snap.manual_resume_required and bot is not None:
        if st.button("✅ Confirm Risk Resume", use_container_width=True):
            msg = bot.confirm_risk_resume()
            st.success(msg)
            st.rerun()

    st.divider()

    if not _AUTOREFRESH:
        st.caption("Tip: `pip install streamlit-autorefresh` for live updates.")
        if st.button("Manual Refresh", use_container_width=True):
            st.rerun()


# --------------------------------------------------------------------------- #
# Main page — essential metrics + trade log + chart                           #
# --------------------------------------------------------------------------- #
st.title("BTC/USDT ML Futures Desk")
st.caption(config.execution_banner_text())

log_df = load_log()
trades_df = load_trades()
stats = compute_stats(log_df, trades_df)
exchange_pos = fetch_live_position()

for warning in stats.get("data_warnings", []):
    st.warning(warning)

bot = get_bot()
render_bot_heartbeat(bot, log_df)
render_essential_metrics(trades_df, log_df, exchange_pos=exchange_pos, bot=bot)
render_compound_strip(trades_df, log_df, bot=bot)

log_col, chart_col = st.columns([1.1, 1])
with log_col:
    render_trade_log(trades_df, max_rows=50)
with chart_col:
    st.markdown(
        '<div class="trade-log-title" style="margin-bottom:10px;">Chart</div>',
        unsafe_allow_html=True,
    )
    render_tradingview(symbol="BINANCE:BTCUSDT.P", height=420)

with st.expander("Model thresholds & probabilities", expanded=False):
    long_thr, short_thr, thr_source = get_live_thresholds()
    st.caption(f"Thresholds: {thr_source}")
    st.progress(stats["prob_long"], text=f"LONG {stats['prob_long']*100:.1f}% (thr {long_thr:.0%})")
    st.progress(stats["prob_short"], text=f"SHORT {stats['prob_short']*100:.1f}% (thr {short_thr:.0%})")

st.caption(
    f"Auto-refresh: {'on' if _AUTOREFRESH else 'manual'} · "
    f"Store: {os.path.basename(config.DB_FILE)} · "
    f"SH = hold minutes · ENTRY/EXIT in USDT"
)
