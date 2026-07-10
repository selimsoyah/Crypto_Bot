from __future__ import annotations

import html
import os
from typing import Optional

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import config
import bot_runtime
import dashboard_stats
import exchange_client

DESK_PANEL_CSS = """
html, body {
    margin: 0;
    padding: 0;
    background: transparent;
    font-family: "Source Sans Pro", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: #f8fafc;
}
.desk-panel {
    border: 1px solid rgba(148, 163, 184, 0.22);
    border-radius: 14px;
    background: linear-gradient(165deg, rgba(15, 23, 42, 0.88), rgba(2, 6, 23, 0.94));
    box-shadow: 0 10px 28px rgba(2, 6, 23, 0.5);
    padding: 14px 14px 12px;
    margin-bottom: 4px;
    box-sizing: border-box;
}
.desk-section-title {
    font-size: 0.72rem;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: .08em;
    margin: 0 0 10px 2px;
}
.desk-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
}
.desk-grid.six {
    grid-template-columns: repeat(6, minmax(0, 1fr));
}
.desk-card {
    border: 1px solid rgba(148, 163, 184, 0.2);
    border-radius: 12px;
    padding: 12px 14px;
    background: rgba(15, 23, 42, 0.55);
    min-height: 96px;
    box-sizing: border-box;
}
.desk-card.pos { border-color: rgba(34, 197, 94, 0.45); background: rgba(34, 197, 94, 0.08); }
.desk-card.neg { border-color: rgba(239, 68, 68, 0.45); background: rgba(239, 68, 68, 0.08); }
.desk-card.side-long { border-color: rgba(34, 197, 94, 0.5); }
.desk-card.side-short { border-color: rgba(239, 68, 68, 0.5); }
.desk-title { font-size: 0.72rem; color: #94a3b8; text-transform: uppercase; letter-spacing: .06em; }
.desk-odo {
    font-size: 1.45rem;
    font-weight: 800;
    margin-top: 6px;
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
}
.desk-odo.pos { color: #22c55e; }
.desk-odo.neg { color: #ef4444; }
.desk-odo.neutral { color: #f8fafc; }
.desk-odo.long { color: #22c55e; }
.desk-odo.short { color: #ef4444; }
.desk-sub { font-size: 0.76rem; color: #a8b1bf; margin-top: 6px; }
.desk-empty {
    border: 1px dashed rgba(148, 163, 184, 0.35);
    border-radius: 10px;
    padding: 14px;
    color: #94a3b8;
    font-size: 0.86rem;
    text-align: center;
}
@media (max-width: 1100px) {
    .desk-grid.six { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 700px) {
    .desk-grid, .desk-grid.six { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
"""

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
.profile-wrap {
    border: 1px solid rgba(148, 163, 184, 0.20);
    border-radius: 12px;
    background: linear-gradient(160deg, rgba(15, 23, 42, 0.72), rgba(2, 6, 23, 0.8));
    padding: 10px 12px;
    margin-bottom: 10px;
}
.profile-kicker {
    font-size: 0.66rem;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: .08em;
    margin-bottom: 4px;
}
.profile-title {
    font-size: 1.02rem;
    font-weight: 700;
    color: #f8fafc;
    margin-bottom: 6px;
}
.profile-sub {
    font-size: 0.80rem;
    color: #cbd5e1;
    margin-top: 2px;
}
.box-grid {
    margin-top: 8px;
    display: grid;
    grid-template-columns: repeat(4, minmax(120px, 1fr));
    gap: 8px;
}
.box-cell {
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 10px;
    padding: 8px 10px;
    background: rgba(15, 23, 42, 0.45);
}
.box-k { font-size: 0.66rem; color: #94a3b8; text-transform: uppercase; letter-spacing: .06em; }
.box-v { font-size: 1rem; font-weight: 700; margin-top: 2px; color: #f8fafc; }
.box-v.pos { color: #22c55e; }
.box-v.neg { color: #ef4444; }
.box-v.warn { color: #f59e0b; }
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
.row-scan td {
    color: #7dd3fc;
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


@st.cache_data(ttl=max(1, REFRESH_MS // 1000), show_spinner=False)
def load_chart_candles() -> pd.DataFrame:
    import data_pipeline

    return data_pipeline.fetch_latest_candles(limit=max(300, config.LIVE_CANDLE_LOOKBACK))


def render_darvas_box_sidebar(box_stats: dict) -> None:
    if not config.is_darvas_box_profile():
        return
    st.divider()
    st.markdown("### Darvas Box")
    if not box_stats.get("valid"):
        st.caption(box_stats.get("reason", "Waiting for previous-day OHLCV data."))
        return
    st.metric("Active Box ID", int(box_stats.get("active_box_number", 0)))
    c1, c2 = st.columns(2)
    c1.metric("Box Top", f"{float(box_stats.get('box_top', 0.0)):,.2f}")
    c2.metric("Box Bottom", f"{float(box_stats.get('box_bottom', 0.0)):,.2f}")
    st.metric("Middle Line", f"{float(box_stats.get('middle_line', 0.0)):,.2f}")
    prev_day = box_stats.get("prev_day", "")
    if prev_day:
        st.caption(f"Anchored from previous UTC day: {prev_day}")


def render_darvas_price_chart(candles: pd.DataFrame, box_stats: dict) -> None:
    import plotly.graph_objects as go

    if candles is None or candles.empty:
        st.warning("No candle data available for Darvas box chart.")
        return

    frame = candles.copy()
    if "Timestamp" in frame.columns:
        frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["Timestamp"])
    if frame.empty:
        st.warning("Candle timestamps unavailable for chart.")
        return

    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=frame["Timestamp"],
            open=frame["Open"],
            high=frame["High"],
            low=frame["Low"],
            close=frame["Close"],
            name="BTC/USDT",
            increasing_line_color="#22c55e",
            decreasing_line_color="#ef4444",
        )
    )

    if box_stats.get("valid"):
        top = float(box_stats["box_top"])
        middle = float(box_stats["middle_line"])
        bottom = float(box_stats["box_bottom"])
        x0 = frame["Timestamp"].iloc[0]
        x1 = frame["Timestamp"].iloc[-1]
        lines = (
            (top, "#f08080", "solid", "Box Top"),
            (middle, "#e5e7eb", "dash", "Middle"),
            (bottom, "#7dd3fc", "solid", "Box Bottom"),
        )
        for y_val, color, dash, label in lines:
            fig.add_shape(
                type="line",
                x0=x0,
                x1=x1,
                y0=y_val,
                y1=y_val,
                line=dict(color=color, width=2, dash=dash),
                layer="above",
            )
            fig.add_annotation(
                x=x1,
                y=y_val,
                text=f"{label} {y_val:,.2f}",
                showarrow=False,
                xanchor="left",
                xshift=6,
                font=dict(color=color, size=11),
            )

    box_id = int(box_stats.get("active_box_number", 0))
    title = f"15m BTC/USDT · Darvas box #{box_id}" if box_id else "15m BTC/USDT · Darvas box"
    fig.update_layout(
        template="plotly_dark",
        height=480,
        margin=dict(l=8, r=8, t=36, b=8),
        xaxis_rangeslider_visible=False,
        title=title,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)


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
def get_risk_engine():
    from risk_engine import RiskEngine

    return RiskEngine()


def _handle_headless_shutdown() -> None:
    from bot_loop import recover_orphan_session_export, render_session_report_pdf

    ok, msg = bot_runtime.stop_engine_service()
    if not ok:
        st.error(msg)
        return
    bot_runtime.wait_for_engine_stop()
    report = recover_orphan_session_export(get_store())
    if report is None:
        st.warning("Engine stopped, but no active session report was found.")
        return
    pdf_bytes = render_session_report_pdf(report)
    ts = report.summary["shutdown_ts"].strftime("%Y-%m-%d_%H%M%S")
    filename = f"session_summary_{ts}.pdf"
    st.session_state["pending_session_pdf"] = (pdf_bytes, filename)
    csv_path = getattr(report, "csv_path", None)
    csv_name = os.path.basename(csv_path) if csv_path else None
    st.session_state["shutdown_notice"] = (filename, csv_name)


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


def render_profile_badge(box_stats: Optional[dict] = None) -> None:
    active = str(getattr(config, "ACTIVE_PROFILE", "xgboost_ml")).strip()
    title = "Darvas Box Breakout" if active == "darvas_box" else "XGBoost ML Inference"
    st.markdown(
        "<div class='profile-wrap'>"
        "<div class='profile-kicker'>Active Runtime Profile</div>"
        f"<div class='profile-title'>{html.escape(title)}</div>"
        f"<div class='profile-sub'>{html.escape(config.profile_summary())}</div>",
        unsafe_allow_html=True,
    )
    if active != "darvas_box":
        st.markdown("</div>", unsafe_allow_html=True)
        return

    box_stats = box_stats or {}
    if box_stats.get("valid"):
        breakout = str(box_stats.get("breakout", "CASH")).upper()
        tone = "warn"
        if breakout == "LONG":
            tone = "pos"
        elif breakout == "SHORT":
            tone = "neg"
        box_id = int(box_stats.get("active_box_number", 0))
        middle = float(box_stats.get("middle_line", 0.0))
        st.markdown(
            "<div class='box-grid'>"
            f"<div class='box-cell'><div class='box-k'>Active Box</div><div class='box-v'>#{box_id}</div></div>"
            f"<div class='box-cell'><div class='box-k'>Box Top</div><div class='box-v'>{float(box_stats.get('box_top', 0.0)):,.2f}</div></div>"
            f"<div class='box-cell'><div class='box-k'>Middle Line</div><div class='box-v'>{middle:,.2f}</div></div>"
            f"<div class='box-cell'><div class='box-k'>Box Bottom</div><div class='box-v'>{float(box_stats.get('box_bottom', 0.0)):,.2f}</div></div>"
            f"<div class='box-cell'><div class='box-k'>Box Height</div><div class='box-v'>{float(box_stats.get('box_height', 0.0)):,.2f}</div></div>"
            f"<div class='box-cell'><div class='box-k'>Breakout</div><div class='box-v {tone}'>{html.escape(breakout)}</div></div>"
            "</div>"
            f"<div class='profile-sub'>Prev UTC day anchor · RR {config.BOX_RISK_REWARD_RATIO:.2f} · "
            f"Vol x{config.BOX_VOLUME_FILTER_MULTIPLIER:.2f}</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        "<div class='profile-sub'>Waiting for previous UTC day OHLCV to anchor today's box boundaries.</div>"
        "</div>",
        unsafe_allow_html=True,
    )


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
    if not config.is_xgboost_ml_profile():
        st.caption(
            "Darvas Box profile active — ML probability thresholds are not used."
        )
        return
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


def _pnl_tone(value: float) -> str:
    if value > 0:
        return "pos"
    if value < 0:
        return "neg"
    return "neutral"


def _text_card(title: str, text: str, *, tone: str = "neutral", card_cls: str = "", sub: str = "") -> str:
    sub_html = f'<div class="desk-sub">{html.escape(sub)}</div>' if sub else ""
    return (
        f'<div class="desk-card {card_cls}">'
        f'<div class="desk-title">{html.escape(title)}</div>'
        f'<div class="desk-odo {tone}">{html.escape(text)}</div>'
        f"{sub_html}"
        f"</div>"
    )


def _odo_card(
    key: str,
    title: str,
    value: float,
    *,
    decimals: int = 2,
    prefix: str = "",
    suffix: str = "",
    tone: str = "neutral",
    sub: str = "",
    card_cls: str = "",
) -> str:
    card_tone = card_cls or tone
    sub_html = f'<div class="desk-sub">{html.escape(sub)}</div>' if sub else ""
    return (
        f'<div class="desk-card {card_tone}">'
        f'<div class="desk-title">{html.escape(title)}</div>'
        f'<div id="odo-{html.escape(key)}" class="desk-odo {tone}" '
        f'data-key="{html.escape(key)}" data-target="{value}" data-decimals="{decimals}" '
        f'data-prefix="{html.escape(prefix)}" data-suffix="{html.escape(suffix)}" '
        f'data-signed="{"1" if tone in ("pos", "neg") else "0"}"></div>'
        f"{sub_html}"
        f"</div>"
    )


def _odo_panel_html(sections: list[str], height: int) -> None:
    body = "".join(sections)
    panel = f"""
    <style>{DESK_PANEL_CSS}</style>
    <div class="desk-panel">{body}</div>
    <script>
    (function() {{
      function setFrameHeight() {{
        const h = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
        window.parent.postMessage({{type: "streamlit:setFrameHeight", height: h}}, "*");
      }}
      function fmt(v, d, prefix, suffix, signed) {{
        const n = Number(v);
        const sign = signed && n > 0 ? "+" : "";
        return `${{prefix}}${{sign}}${{n.toLocaleString(undefined, {{minimumFractionDigits:d, maximumFractionDigits:d}})}}${{suffix}}`;
      }}
      document.querySelectorAll('[id^="odo-"]').forEach((el) => {{
        const k = el.dataset.key;
        const target = Number(el.dataset.target || 0);
        const d = Number(el.dataset.decimals || 0);
        const prefix = el.dataset.prefix || "";
        const suffix = el.dataset.suffix || "";
        const signed = el.dataset.signed === "1";
        const sk = "desk_odo_" + k;
        const prev = Number(sessionStorage.getItem(sk) || target);
        const start = Number.isFinite(prev) ? prev : target;
        const delta = target - start;
        const startTs = performance.now();
        const dur = 900;
        function tick(ts) {{
          const t = Math.min(1, (ts - startTs) / dur);
          const eased = 1 - Math.pow(1 - t, 3);
          const val = start + delta * eased;
          el.textContent = fmt(val, d, prefix, suffix, signed);
          if (t < 1) requestAnimationFrame(tick);
          else sessionStorage.setItem(sk, String(target));
        }}
        requestAnimationFrame(tick);
      }});
      setFrameHeight();
      window.addEventListener("load", setFrameHeight);
      setTimeout(setFrameHeight, 950);
    }})();
    </script>
    """
    components.html(panel, height=height, scrolling=False)


def render_compound_and_position(
    comp: Optional[dict],
    exchange_pos: Optional[dict],
    log_df: Optional[pd.DataFrame] = None,
    runtime_snapshot: Optional[dict] = None,
) -> None:
    sections: list[str] = []

    if comp is not None:
        pnl_tone = _pnl_tone(float(comp["pnl_7d"]))
        exp_tone = _pnl_tone(float(comp["expectancy"]))
        compound_cards = (
            _odo_card("7d_trades", "7d Trades", float(comp["trades_7d"]), decimals=0, tone="neutral")
            + _odo_card(
                "7d_pnl",
                "7d PnL",
                float(comp["pnl_7d"]),
                tone=pnl_tone,
                card_cls=pnl_tone,
            )
            + _odo_card(
                "expectancy",
                "Expectancy",
                float(comp["expectancy"]),
                tone=exp_tone,
                card_cls=exp_tone,
            )
            + _odo_card(
                "size_mult",
                "Size Mult",
                float(comp["size_mult"]),
                decimals=2,
                suffix="x",
                tone="neutral",
            )
        )
        sections.append(
            '<div class="desk-section-title">7-Day Performance</div>'
            f'<div class="desk-grid">{compound_cards}</div>'
        )

    sections.append('<div class="desk-section-title" style="margin-top:14px;">Live Position</div>')

    if not exchange_pos or exchange_pos.get("status") == "flat":
        sections.append('<div class="desk-empty">No open exchange position.</div>')
    elif exchange_pos.get("status") == "error":
        msg = html.escape(str(exchange_pos.get("message", "unknown error")))
        sections.append(f'<div class="desk-empty">Could not fetch exchange position: {msg}</div>')
    else:
        side = str(exchange_pos.get("side", "FLAT")).upper()
        side_tone = "long" if side == "LONG" else ("short" if side == "SHORT" else "neutral")
        side_card = "side-long" if side == "LONG" else ("side-short" if side == "SHORT" else "")
        pnl = float(exchange_pos.get("unrealized_pnl", 0.0) or 0.0)
        pct = float(exchange_pos.get("pct_change", 0.0) or 0.0)
        pnl_tone = _pnl_tone(pnl)
        pct_tone = _pnl_tone(pct)

        tp = sl = 0.0
        pos = {}
        if isinstance(runtime_snapshot, dict):
            pos = runtime_snapshot.get("position", {}) if isinstance(runtime_snapshot.get("position"), dict) else {}
        if pos.get("tp_price") and pos.get("sl_price"):
            tp = float(pos.get("tp_price", 0.0) or 0.0)
            sl = float(pos.get("sl_price", 0.0) or 0.0)
        elif log_df is not None and not log_df.empty:
            last = log_df.iloc[-1]
            tp = float(last.get("TP_Price", 0.0) or 0.0)
            sl = float(last.get("SL_Price", 0.0) or 0.0)
        footer = (
            f"TP ${tp:,.2f} · SL ${sl:,.2f}"
            if tp and sl
            else "TP/SL managed by bot when position is internal."
        )

        position_cards = (
            _text_card("Side", side, tone=side_tone, card_cls=side_card)
            + _odo_card(
                "pos_qty",
                "Qty",
                float(exchange_pos.get("quantity", 0.0) or 0.0),
                decimals=4,
                suffix=" BTC",
                tone="neutral",
            )
            + _odo_card(
                "pos_entry",
                "Entry",
                float(exchange_pos.get("entry_price", 0.0) or 0.0),
                decimals=2,
                prefix="$",
                tone="neutral",
            )
            + _odo_card(
                "pos_mark",
                "Mark",
                float(exchange_pos.get("mark_price", 0.0) or 0.0),
                decimals=2,
                prefix="$",
                tone="neutral",
            )
            + _odo_card(
                "pos_upnl",
                "Unrealized",
                pnl,
                decimals=2,
                prefix="$",
                tone=pnl_tone,
                card_cls=pnl_tone,
            )
            + _odo_card(
                "pos_upnl_pct",
                "Unrealized %",
                pct,
                decimals=2,
                suffix="%",
                tone=pct_tone,
                card_cls=pct_tone,
            )
        )
        sections.append(f'<div class="desk-grid six">{position_cards}</div>')
        sections.append(f'<div class="desk-sub" style="margin:10px 2px 0;">{html.escape(footer)}</div>')

    has_position = bool(exchange_pos and exchange_pos.get("status") not in (None, "flat", "error"))
    height = 300 if comp is not None and has_position else (220 if has_position else 180)
    _odo_panel_html(sections, height=height)


def render_activity(log_df: pd.DataFrame) -> None:
    st.subheader("Session Activity")
    st.caption(
        f"Live scan feed · auto-refresh every {REFRESH_MS // 1000}s · "
        "SCAN rows prove the engine is breathing"
    )
    rows = dashboard_stats.status_to_activity_rows(log_df, max_rows=50)
    if not rows:
        st.info("No activity rows yet — boot the engine to start scan heartbeats.")
        return
    body = []
    for r in rows:
        reason = html.escape(str(r.get("reason", "")))
        action = html.escape(str(r.get("action", "")))
        tone = str(r.get("tone", "info"))
        if "ERROR" in action or "FAILED" in action:
            cls = "row-bad"
        elif tone == "scan":
            cls = "row-scan"
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


def render_sidebar_controls(
    engine_status: dict,
    box_stats: Optional[dict] = None,
):
    risk_engine = get_risk_engine()
    risk_flags = bot_runtime.risk_flags_from_runtime(engine_status)
    with st.sidebar:
        st.markdown("## Control")
        st.caption(config.execution_banner_text())
        st.caption(
            f"{config.SYMBOL} · {config.INTERVAL} · {config.LEVERAGE}x · "
            + (
                (
                    f"Prev UTC day box · RR {config.BOX_RISK_REWARD_RATIO:.2f} · "
                    f"Vol x{config.BOX_VOLUME_FILTER_MULTIPLIER:.2f}"
                )
                if config.is_darvas_box_profile()
                else f"TP +{config.TAKE_PROFIT_PCT:.1%} / SL -{config.STOP_LOSS_PCT:.1%}"
            )
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
            elif engine_status.get("running"):
                st.info("Engine is already running.")
            elif engine_status.get("booting"):
                st.info("Engine is booting — status will update shortly.")
            elif engine_status.get("systemd_available"):
                ok, msg = bot_runtime.start_engine_service()
                if ok:
                    booted = bot_runtime.wait_for_engine_start()
                    if booted:
                        st.success("Engine is online.")
                    else:
                        st.warning(
                            "Engine start requested but no live heartbeat yet. "
                            "Check `sudo journalctl -u crypto-bot -n 40`."
                        )
                    st.rerun()
                else:
                    st.error(msg)
            else:
                st.error(
                    "Headless engine service is not installed. Run "
                    "`bash deploy/scripts/install_server.sh` on the server."
                )

        if stop:
            if engine_status.get("running") or engine_status.get("process_alive"):
                _handle_headless_shutdown()
                st.rerun()
            else:
                st.warning("Engine is already offline.")

        running = bool(engine_status.get("running"))
        booting = bool(engine_status.get("booting"))
        mode = str(engine_status.get("mode", "offline"))
        pid = int(engine_status.get("pid", 0) or 0)
        if running:
            st.success(f"ENGINE RUNNING ({mode}, pid {pid})")
        elif booting:
            st.warning(f"ENGINE BOOTING ({mode})")
        elif engine_status.get("process_alive"):
            st.warning("ENGINE STALE (process alive, heartbeat delayed)")
        else:
            st.error("ENGINE OFFLINE")
            service_state = str(engine_status.get("service_state", "") or "")
            if service_state == "failed":
                st.caption("crypto-bot service failed — inspect journalctl logs.")

        if engine_status.get("degraded"):
            st.warning(
                f"API degraded: {engine_status.get('connection_error') or 'recent failures'}"
            )
        last_error = str(engine_status.get("last_error", "") or "")
        if last_error and not running:
            st.caption(f"Last error: {last_error}")

        st.divider()
        st.markdown("### Risk")
        st.caption(
            f"Max session loss: {config.RISK_MAX_DAILY_LOSS_PCT:.1%} · "
            f"Max consecutive losses: {config.RISK_MAX_CONSECUTIVE_LOSSES}"
        )

        if risk_flags["kill_switch_active"]:
            st.error(f"Kill switch ACTIVE ({config.KILL_SWITCH_FILE})")
        elif risk_flags["halted"]:
            st.warning(risk_flags["halt_reason"] or "Risk engine halted.")
        elif risk_flags["manual_resume_required"]:
            st.warning("Consecutive-loss pause — manual resume required.")
        else:
            st.success("Risk gates OK")

        k1, k2 = st.columns(2)
        engage_kill = k1.button("☠️ KILL", use_container_width=True)
        clear_kill = k2.button("Clear", use_container_width=True)

        if engage_kill:
            risk_engine.trigger_kill_switch("Dashboard kill switch engaged")
            if engine_status.get("running") or engine_status.get("process_alive"):
                _handle_headless_shutdown()
            st.rerun()

        if clear_kill:
            risk_engine.clear_kill_switch()
            st.success("Kill switch cleared.")
            st.rerun()

        if risk_flags["manual_resume_required"]:
            if st.button("✅ Confirm Risk Resume", use_container_width=True):
                st.success(risk_engine.confirm_manual_resume().reason)
                st.rerun()

        st.divider()
        if box_stats is not None:
            render_darvas_box_sidebar(box_stats)
        if not _AUTOREFRESH and st.button("Manual Refresh", use_container_width=True):
            st.rerun()


if st.session_state.get("pending_session_pdf"):
    _pdf_bytes, _pdf_name = st.session_state.pop("pending_session_pdf")
    _auto_download_pdf(_pdf_bytes, _pdf_name)

_show_shutdown_notice()

log_df = load_log()
trades_df = load_trades()
engine_status = bot_runtime.engine_runtime_status(log_df)
runtime_snapshot = engine_status.get("snapshot", {}) or {}

chart_candles = pd.DataFrame()
darvas_box_stats: dict = {}
if config.is_darvas_box_profile():
    try:
        chart_candles = load_chart_candles()
        darvas_box_stats = dashboard_stats.darvas_box_stats(
            chart_candles,
            runtime=engine_status,
        )
    except Exception as exc:
        darvas_box_stats = {"valid": False, "reason": f"Could not load box data: {exc}"}

render_sidebar_controls(
    engine_status=engine_status,
    box_stats=darvas_box_stats if config.is_darvas_box_profile() else None,
)

st.title("BTC/USDT ML Futures Desk")
render_venue_banner()
render_profile_badge(darvas_box_stats if config.is_darvas_box_profile() else None)

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

health = dashboard_stats.bot_health(
    None,
    log_df,
    exchange_pos=exchange_pos,
    runtime=engine_status,
)
st.info(
    f"Engine: {health['status']} · Last scan: {health['last_scan']} · "
    f"Action: {health['last_action']} · Position: {health['open_position']}"
)
st.caption(health["detail"])

metrics = dashboard_stats.essential_metrics(
    trades_df,
    log_df,
    exchange_pos=exchange_pos,
    runtime_snapshot=runtime_snapshot,
)
render_threshold_circles(stats)
render_top_metrics(metrics)

if config.is_compound_profile():
    comp = dashboard_stats.compute_compound_metrics(
        trades_df,
        runtime_snapshot=runtime_snapshot,
    )
else:
    comp = None
render_compound_and_position(
    comp,
    exchange_pos,
    log_df=log_df,
    runtime_snapshot=runtime_snapshot,
)

left, right = st.columns([1, 1], gap="large")
with left:
    render_activity(log_df)
with right:
    render_closed_trades(trades_df, exchange_pos)

st.subheader("Chart")
if config.is_darvas_box_profile():
    render_darvas_price_chart(chart_candles, darvas_box_stats)
    with st.expander("TradingView reference", expanded=False):
        render_tradingview(symbol="BINANCE:BTCUSDT.P", height=420)
else:
    render_tradingview(symbol="BINANCE:BTCUSDT.P", height=480)

with st.expander("Model thresholds & probabilities", expanded=False):
    render_threshold_circles(stats)

st.caption(
    f"Auto-refresh: {'on' if _AUTOREFRESH else 'manual'} · "
    f"Store: {os.path.basename(config.DB_FILE)}"
)
