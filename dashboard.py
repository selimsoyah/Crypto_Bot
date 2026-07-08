from __future__ import annotations

import html
import os
from typing import Optional

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import config
import dashboard_stats
import exchange_client

st.set_page_config(
    page_title="BTC/USDT ML Futures Desk",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
.block-container {padding-top: 1rem; padding-bottom: 1.5rem; max-width: 1600px;}
.metric-card {
    border: 1px solid rgba(148, 163, 184, 0.25);
    border-radius: 12px;
    padding: 14px 16px;
    background: linear-gradient(160deg, rgba(15, 23, 42, 0.82), rgba(2, 6, 23, 0.9));
    min-height: 110px;
    box-shadow: 0 8px 20px rgba(2, 6, 23, 0.45);
}
.metric-title {font-size: 0.78rem; color: #94a3b8; text-transform: uppercase; letter-spacing: .06em;}
.metric-value {font-size: 1.6rem; font-weight: 800; margin-top: 4px; font-variant-numeric: tabular-nums;}
.metric-sub {font-size: 0.82rem; color: #a8b1bf; margin-top: 4px;}
.pos { color: #22c55e; }
.neg { color: #ef4444; }
.subtle { color: #94a3b8; }
.good { color: #22c55e; }
.warn { color: #f59e0b; }
.bad  { color: #ef4444; }
.thr-wrap {
    border: 1px solid rgba(148, 163, 184, 0.20);
    border-radius: 12px;
    background: linear-gradient(160deg, rgba(15, 23, 42, 0.72), rgba(2, 6, 23, 0.8));
    box-shadow: 0 8px 20px rgba(2, 6, 23, 0.35);
    padding: 10px 8px;
    margin-bottom: 10px;
}
.thr-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(120px, 1fr));
    gap: 8px;
}
.thr-card {
    display: flex;
    align-items: center;
    gap: 8px;
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 10px;
    padding: 8px;
    background: rgba(15, 23, 42, 0.45);
}
.thr-title { font-size: 0.68rem; color: #94a3b8; text-transform: uppercase; letter-spacing: .06em; }
.thr-val { font-size: 1rem; font-weight: 700; font-variant-numeric: tabular-nums; }
.thr-sub { font-size: 0.70rem; color: #a8b1bf; margin-top: 2px; }
.thr-svg { width: 54px; height: 54px; flex-shrink: 0; }
.log-wrap {
    border: 1px solid rgba(148, 163, 184, 0.22);
    border-radius: 12px;
    overflow: hidden;
    background: rgba(2, 6, 23, 0.45);
}
.log-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
    font-variant-numeric: tabular-nums;
}
.log-table th {
    text-align: left;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: #94a3b8;
    background: rgba(15, 23, 42, 0.9);
    padding: 8px 10px;
}
.log-table td {
    padding: 8px 10px;
    border-top: 1px solid rgba(148, 163, 184, 0.14);
}
.row-good td { color: #22c55e; }
.row-warn td { color: #f59e0b; }
.row-bad  td { color: #ef4444; }
.row-live td {
    color: #22c55e;
    animation: liveBlink 1.1s ease-in-out infinite;
}
@keyframes liveBlink {
    0%, 100% { background: rgba(34, 197, 94, 0.06); }
    50% { background: rgba(34, 197, 94, 0.25); }
}
</style>
""",
    unsafe_allow_html=True,
)

REFRESH_MS = 5000
try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(interval=REFRESH_MS, key="auto_refresh")
    _AUTOREFRESH = True
except Exception:
    _AUTOREFRESH = False


@st.cache_resource(show_spinner=False)
def get_store():
    from trade_store import TradeStore

    return TradeStore()


@st.cache_resource(show_spinner=False)
def _futures_client():
    try:
        return exchange_client.build_execution_client()
    except Exception:
        return None


@st.cache_data(ttl=max(1, REFRESH_MS // 1000), show_spinner=False)
def fetch_live_position(symbol: str = config.SYMBOL) -> dict:
    return dashboard_stats.fetch_live_position(symbol=symbol, client=_futures_client())


def load_log(limit: int = 3000) -> pd.DataFrame:
    try:
        return get_store().read_status_df(limit=limit)
    except Exception as exc:
        st.sidebar.error(f"Could not read status log: {exc}")
        return pd.DataFrame(
            columns=[
                "Timestamp",
                "Current_Price",
                "Prob_Long",
                "Prob_Short",
                "Prob_Cash",
                "Direction",
                "Current_Balance",
                "Open_Position",
                "Realized_PNL",
                "Unrealized_PNL",
                "Entry_Price",
                "TP_Price",
                "SL_Price",
                "Action",
                "Event",
                "Reason",
                "Session_Id",
            ]
        )


def load_trades() -> pd.DataFrame:
    try:
        return get_store().read_trades_df()
    except Exception as exc:
        st.sidebar.error(f"Could not read trades ledger: {exc}")
        return pd.DataFrame()


def get_live_thresholds() -> tuple[float, float, str]:
    """Thresholds the bot actually trades with (same resolver as bot_loop)."""
    from bot_loop import resolve_live_thresholds

    return resolve_live_thresholds()


@st.cache_resource(show_spinner=False)
def _bot_singleton() -> tuple:
    try:
        from bot_loop import TradingBot

        return TradingBot(), ""
    except Exception as exc:
        return None, str(exc)


def get_bot():
    bot, err = _bot_singleton()
    if err:
        st.session_state.bot_error = err
    return bot


def _auto_download_pdf(pdf_bytes: bytes, filename: str) -> None:
    import base64

    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    components.html(
        f"""
        <script>
        (function() {{
            const b64 = {b64!r};
            const filename = {filename!r};
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
    from bot_loop import render_session_report_pdf

    report = bot.stop()
    if report is None:
        st.warning("Bot stopped, but no active session was found to report.")
        return
    pdf_bytes = render_session_report_pdf(report)
    ts = report.summary["shutdown_ts"].strftime("%Y-%m-%d_%H%M%S")
    filename = f"session_summary_{ts}.pdf"
    st.session_state["pending_session_pdf"] = (pdf_bytes, filename)
    csv_path = getattr(report, "csv_path", None)
    csv_name = os.path.basename(csv_path) if csv_path else None
    st.session_state["shutdown_notice"] = (filename, csv_name)
    _bot_singleton.clear()


def _show_shutdown_notice() -> None:
    notice = st.session_state.pop("shutdown_notice", None)
    if notice:
        if isinstance(notice, tuple):
            pdf_name, csv_name = notice
            csv_line = f" CSV: **{csv_name}** (`session_exports/`)." if csv_name else ""
            st.sidebar.success(
                f"Session ended. PDF: **{pdf_name}**.{csv_line} Markdown: `session_summary_report.md`"
            )
        else:
            st.sidebar.success(f"Session ended. PDF downloaded: **{notice}**")


def render_tradingview(symbol: str = "BINANCE:BTCUSDT.P", height: int = 520) -> None:
    widget = f"""
    <div class="tradingview-widget-container" style="height:{height}px;width:100%">
      <div id="tv_chart" style="height:{height}px;width:100%"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
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


def render_venue_banner() -> None:
    if config.execution_is_live():
        st.error("LIVE MAINNET EXECUTION — REAL CAPITAL AT RISK")
    else:
        st.success("TESTNET EXECUTION — NO REAL FUNDS")
    st.caption(config.execution_banner_text())


def render_top_metrics(metrics: dict) -> None:
    wallet = metrics["wallet_balance"]
    net = metrics["net_pnl"]
    open_pnl = metrics["open_pnl"]
    wr = metrics["win_rate"]
    c1, c2, c3, c4 = st.columns(4, gap="small")
    with c1:
        st.markdown(
            f'<div class="metric-card"><div class="metric-title">Wallet</div>'
            f'<div class="metric-value pos">${wallet:,.2f}</div>'
            f'<div class="metric-sub">USDT margin</div></div>',
            unsafe_allow_html=True,
        )
    with c2:
        cls = "pos" if net >= 0 else "neg"
        sign = "+" if net >= 0 else ""
        st.markdown(
            f'<div class="metric-card"><div class="metric-title">Net Closed PnL</div>'
            f'<div class="metric-value {cls}">{sign}{net:.2f}</div>'
            f'<div class="metric-sub">{metrics["total_trades"]} closed trades</div></div>',
            unsafe_allow_html=True,
        )
    with c3:
        cls = "pos" if open_pnl >= 0 else "neg"
        sign = "+" if open_pnl >= 0 else ""
        st.markdown(
            f'<div class="metric-card"><div class="metric-title">Open PnL</div>'
            f'<div class="metric-value {cls}">{sign}{open_pnl:.2f}</div>'
            f'<div class="metric-sub">{metrics["open_side"]}</div></div>',
            unsafe_allow_html=True,
        )
    with c4:
        cls = "pos" if wr >= 50 else ("neg" if metrics["total_trades"] else "subtle")
        st.markdown(
            f'<div class="metric-card"><div class="metric-title">Win Rate</div>'
            f'<div class="metric-value {cls}">{wr:.1f}%</div>'
            f'<div class="metric-sub">{metrics["wins"]}W / {metrics["losses"]}L</div></div>',
            unsafe_allow_html=True,
        )


def _circle_card(label: str, value: float, threshold: float, color: str) -> str:
    pct = max(0.0, min(100.0, value * 100.0))
    circ = 2 * 3.14159 * 20
    offset = circ * (1 - pct / 100.0)
    return (
        f'<div class="thr-card">'
        f'<svg class="thr-svg" viewBox="0 0 54 54">'
        f'<circle cx="27" cy="27" r="20" stroke="rgba(148,163,184,0.30)" stroke-width="5" fill="none"></circle>'
        f'<circle cx="27" cy="27" r="20" stroke="{color}" stroke-width="5" fill="none" '
        f'stroke-linecap="round" transform="rotate(-90 27 27)" '
        f'stroke-dasharray="{circ:.2f}" stroke-dashoffset="{offset:.2f}"></circle>'
        f'<text x="27" y="31" text-anchor="middle" fill="{color}" style="font-size:10px;font-weight:700">{pct:.0f}%</text>'
        f'</svg>'
        f'<div><div class="thr-title">{html.escape(label)}</div>'
        f'<div class="thr-val" style="color:{color}">{pct:.1f}%</div>'
        f'<div class="thr-sub">thr {threshold*100:.1f}%</div></div>'
        f'</div>'
    )


def render_threshold_circles(stats: dict) -> None:
    long_thr, short_thr, thr_source = get_live_thresholds()
    cards_html = (
        _circle_card("Long", float(stats.get("prob_long", 0.0)), long_thr, "#22c55e")
        + _circle_card("Short", float(stats.get("prob_short", 0.0)), short_thr, "#ef4444")
        + _circle_card("Cash", float(stats.get("prob_cash", 0.0)), 0.0, "#f59e0b")
    )
    st.markdown(
        f'<div class="thr-wrap"><div class="thr-title" style="margin-bottom:6px;">Live Thresholds · {html.escape(thr_source)}</div>'
        f'<div class="thr-grid">{cards_html}</div></div>',
        unsafe_allow_html=True,
    )


def render_position_panel(exchange_pos: Optional[dict], bot=None) -> None:
    st.subheader("Live Position")
    if not exchange_pos or exchange_pos.get("status") == "flat":
        st.info("No open exchange position.")
        return
    if exchange_pos.get("status") == "error":
        st.warning(f"Could not fetch exchange position: {exchange_pos.get('message', '')}")
        return

    tp = sl = 0.0
    if bot is not None and bot.state.position is not None:
        tp = bot.state.position.take_profit_price
        sl = bot.state.position.stop_loss_price

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Side", exchange_pos.get("side", "FLAT"))
    c2.metric("Qty", f"{exchange_pos.get('quantity', 0.0):.4f} BTC")
    c3.metric("Entry", f"${exchange_pos.get('entry_price', 0.0):,.2f}")
    c4.metric("Mark", f"${exchange_pos.get('mark_price', 0.0):,.2f}")

    pnl = float(exchange_pos.get("unrealized_pnl", 0.0) or 0.0)
    pct = float(exchange_pos.get("pct_change", 0.0) or 0.0)
    st.metric("Unrealized", f"${pnl:+,.2f}", delta=f"{pct:+.2f}%")
    st.caption(f"TP: ${tp:,.2f} · SL: ${sl:,.2f}" if tp and sl else "TP/SL managed by bot when position is internal.")


def render_activity(log_df: pd.DataFrame) -> None:
    st.subheader("Session Activity")
    rows = dashboard_stats.status_to_activity_rows(log_df, max_rows=30)
    if not rows:
        st.info("No activity rows yet.")
        return
    body = []
    for r in rows:
        reason = html.escape(str(r.get("reason", "")))
        action = html.escape(str(r.get("action", "")))
        tone = str(r.get("tone", "info"))
        if "ERROR" in action or "FAILED" in action:
            cls = "row-bad"
        elif tone == "warn":
            cls = "row-warn"
        else:
            cls = "row-good"
        body.append(
            "<tr class='%s'><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                cls,
                html.escape(str(r.get("time", ""))),
                action,
                html.escape(str(r.get("position", ""))),
                reason,
            )
        )
    st.markdown(
        "<div class='log-wrap'><table class='log-table'>"
        "<thead><tr><th>Time</th><th>Action</th><th>Pos</th><th>Detail</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>",
        unsafe_allow_html=True,
    )


def render_closed_trades(trades_df: pd.DataFrame, exchange_pos: Optional[dict]) -> None:
    st.subheader("Closed Trades")
    rows = dashboard_stats.trades_to_log_rows(trades_df)
    open_row = dashboard_stats.open_position_row(exchange_pos)
    if open_row:
        rows = [open_row, *rows]
    if not rows:
        st.info("No trades yet.")
        return
    table = []
    for idx, r in enumerate(rows):
        is_live = bool(r.get("open"))
        if is_live:
            cls = "row-live"
            status = "OPEN"
        else:
            cls = "row-good" if bool(r.get("won")) else "row-bad"
            status = html.escape(str(r.get("status", "")))
        table.append(
            "<tr class='%s'><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                cls,
                html.escape(str(r.get("time", ""))),
                html.escape(str(r.get("side", ""))),
                f'{float(r.get("entry", 0.0)):,.2f}',
                f'{float(r.get("exit", 0.0)):,.2f}',
                html.escape(str(r.get("sh", ""))),
                status,
                f'{float(r.get("pnl", 0.0)):+.2f}',
            )
        )
    st.markdown(
        "<div class='log-wrap'><table class='log-table'>"
        "<thead><tr><th>Time</th><th>Side</th><th>Entry</th><th>Exit</th><th>SH</th><th>Status</th><th>PnL</th></tr></thead>"
        f"<tbody>{''.join(table)}</tbody></table></div>",
        unsafe_allow_html=True,
    )


def render_sidebar_controls(bot):
    with st.sidebar:
        st.markdown("## Control")
        st.caption(config.execution_banner_text())
        st.caption(
            f"{config.SYMBOL} · {config.INTERVAL} · {config.LEVERAGE}x · "
            f"TP +{config.TAKE_PROFIT_PCT:.1%} / SL -{config.STOP_LOSS_PCT:.1%}"
        )

        if config.execution_is_live():
            st.error("EXECUTION_VENUE=LIVE — type LIVE to confirm boot")
            live_confirm = st.text_input("Confirm LIVE execution", placeholder="Type LIVE")
        else:
            live_confirm = "TESTNET"

        b1, b2 = st.columns(2)
        boot = b1.button("🚀 BOOT", use_container_width=True, type="primary")
        stop = b2.button("🛑 STOP", use_container_width=True)

        if boot:
            if config.execution_is_live() and live_confirm.strip().upper() != "LIVE":
                st.error("Refused to boot: type LIVE to confirm mainnet execution.")
            elif bot is not None:
                if bot.start():
                    st.success("Engine boot sequence initiated.")
                else:
                    st.error(bot.state.last_error or "Refused to boot.")
            else:
                st.error(st.session_state.get("bot_error", "Bot unavailable."))

        if stop:
            if bot is not None:
                _handle_shutdown_with_pdf(bot)
                st.rerun()
            else:
                st.error("Bot unavailable.")

        running = bool(bot and bot.state.running)
        st.success("ENGINE RUNNING" if running else "ENGINE OFFLINE")

        if bot and bot.state.connection_degraded:
            st.warning(f"API degraded: {bot.state.connection_error or 'recent failures'}")
        if bot and bot.state.last_error:
            st.caption(f"Last error: {bot.state.last_error}")

        st.divider()
        st.markdown("### Risk")
        st.caption(
            f"Max session loss: {config.RISK_MAX_DAILY_LOSS_PCT:.1%} · "
            f"Max consecutive losses: {config.RISK_MAX_CONSECUTIVE_LOSSES}"
        )

        risk_snap = bot.risk.snapshot() if bot else None
        kill_file_active = bot.risk.kill_switch_file_active() if bot else os.path.exists(config.KILL_SWITCH_FILE)

        if kill_file_active:
            st.error(f"Kill switch ACTIVE ({config.KILL_SWITCH_FILE})")
        elif risk_snap and risk_snap.halted:
            st.warning(risk_snap.halt_reason or "Risk engine halted.")
        elif risk_snap and risk_snap.manual_resume_required:
            st.warning("Consecutive-loss pause — manual resume required.")
        else:
            st.success("Risk gates OK")

        k1, k2 = st.columns(2)
        engage_kill = k1.button("☠️ KILL", use_container_width=True)
        clear_kill = k2.button("Clear", use_container_width=True)

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
                st.success(bot.confirm_risk_resume())
                st.rerun()

        st.divider()
        if not _AUTOREFRESH and st.button("Manual Refresh", use_container_width=True):
            st.rerun()


if st.session_state.get("pending_session_pdf"):
    _pdf_bytes, _pdf_name = st.session_state.pop("pending_session_pdf")
    _auto_download_pdf(_pdf_bytes, _pdf_name)

_show_shutdown_notice()

bot = get_bot()
render_sidebar_controls(bot)

st.title("BTC/USDT ML Futures Desk")
render_venue_banner()

log_df = load_log()
trades_df = load_trades()
exchange_pos = fetch_live_position()
if (
    exchange_pos.get("status") == "error"
    and "No module named 'binance'" in str(exchange_pos.get("message", ""))
):
    st.warning(
        "Exchange client dependency missing in this Python environment. "
        "Install with: `pip install -r requirements.txt` (or `pip install python-binance`)."
    )

recon = dashboard_stats.reconcile_manual_exchange_close(
    store=get_store(),
    log=log_df,
    trades=trades_df,
    exchange_pos=exchange_pos,
    client=_futures_client(),
)
if recon.get("inserted"):
    log_df = load_log()
    trades_df = load_trades()
    st.success(recon.get("message", "Manual exchange close reconciled."))

stats = dashboard_stats.compute_stats(log_df, trades_df)
mismatch = dashboard_stats.position_mismatch_warning(log_df, exchange_pos)
if mismatch:
    stats.setdefault("data_warnings", []).append(mismatch)
for warning in stats.get("data_warnings", []):
    st.warning(warning)

health = dashboard_stats.bot_health(bot, log_df, exchange_pos=exchange_pos)
st.info(
    f"Engine: {health['status']} · Last scan: {health['last_scan']} · "
    f"Action: {health['last_action']} · Position: {health['open_position']}"
)
st.caption(health["detail"])

metrics = dashboard_stats.essential_metrics(trades_df, log_df, exchange_pos=exchange_pos, bot=bot)
render_threshold_circles(stats)
render_top_metrics(metrics)

if config.is_compound_profile():
    comp = dashboard_stats.compute_compound_metrics(trades_df, bot=bot)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("7d Trades", comp["trades_7d"])
    c2.metric("7d PnL", f"{comp['pnl_7d']:+.2f}")
    c3.metric("Expectancy", f"{comp['expectancy']:+.2f}")
    c4.metric("Size Mult", f"{comp['size_mult']:.2f}x")

render_position_panel(exchange_pos, bot=bot)

left, right = st.columns([1, 1], gap="large")
with left:
    render_activity(log_df)
with right:
    render_closed_trades(trades_df, exchange_pos)

st.subheader("Chart")
render_tradingview(symbol="BINANCE:BTCUSDT.P", height=480)

with st.expander("Model thresholds & probabilities", expanded=False):
    render_threshold_circles(stats)

st.caption(
    f"Auto-refresh: {'on' if _AUTOREFRESH else 'manual'} · "
    f"Store: {os.path.basename(config.DB_FILE)}"
)
