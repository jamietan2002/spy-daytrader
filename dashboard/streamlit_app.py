"""Minimalist Matte-Dark 2-Tab Autonomous Day-Trading Hub (`streamlit run dashboard/streamlit_app.py`).

Linear / Apple Stocks Aesthetic:
- Matte-dark canvas (`#0c0e12`), neutral elevated cards (`#13161c`), crisp tabular numerals.
- Zero emojis, no left sidebar, no manual sync buttons or admin clutter.
- 2 focused tabs:
  1. Today & Open Trade — Paper equity, today's P&L, active position with plain-English reason and automatic Stop-Loss / Profit Target prices, plus today's fills in SGT.
  2. Daily P&L & Journal — Day-by-day P&L history and single-day focused trade log (`← Previous` / `Next →`).
"""

from __future__ import annotations

import html
import os
import sys
from datetime import date
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from spy_daytrader.config import BotConfig  # noqa: E402
from spy_daytrader.dashboard_data import (  # noqa: E402
    DailyPnLRow,
    DashboardSnapshot,
    OpenTradeView,
    OrderJournalRow,
    WorkingEntryView,
    build_daily_takeaway,
    fetch_dashboard_snapshot,
    format_eod_close_sgt,
)

ALPACA_PAPER_PORTAL_URL = "https://app.alpaca.markets/paper/dashboard/overview"

MINIMAL_DARK_CSS = """
<style>
  [data-testid="stSidebar"],
  [data-testid="collapsedControl"],
  [data-testid="stSidebarCollapsedControl"] {
    display: none !important;
  }

  .stApp {
    background-color: #0c0e12 !important;
    color: #f1f5f9 !important;
  }

  .block-container {
    padding-top: 2.0rem !important;
    padding-bottom: 3.5rem !important;
    max-width: 1140px !important;
  }

  html, body, [class*="css"] {
    font-feature-settings: "cv02", "cv03", "cv04", "cv11";
    -webkit-font-smoothing: antialiased;
  }

  /* Linear-style tab bar */
  [data-baseweb="tab-list"] {
    gap: 24px !important;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08) !important;
    padding-bottom: 0px !important;
    background: transparent !important;
  }
  [data-baseweb="tab"] {
    font-size: 0.88rem !important;
    font-weight: 500 !important;
    color: #8b95a5 !important;
    padding: 10px 2px 12px 2px !important;
    background: transparent !important;
  }
  [data-baseweb="tab"][aria-selected="true"] {
    color: #f8fafc !important;
    font-weight: 600 !important;
  }
  [data-baseweb="tab-highlight"] {
    background-color: #f8fafc !important;
    height: 2px !important;
  }

  /* Header */
  .dt-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    padding-bottom: 18px;
    margin-bottom: 8px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.07);
    gap: 16px;
    flex-wrap: wrap;
  }
  .dt-title {
    font-size: 1.35rem;
    font-weight: 600;
    color: #f8fafc;
    letter-spacing: -0.02em;
    margin: 0 0 4px 0;
  }
  .dt-subtitle {
    font-size: 0.82rem;
    color: #94a3b8;
    display: flex;
    align-items: center;
    gap: 8px;
    font-variant-numeric: tabular-nums;
  }
  .dt-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background-color: #94a3b8;
    display: inline-block;
  }
  .dt-dot-live {
    background-color: #f8fafc;
  }
  .dt-ext-link {
    font-size: 0.80rem;
    color: #94a3b8 !important;
    text-decoration: none;
    border: 1px solid rgba(255, 255, 255, 0.08);
    background: #13161c;
    padding: 6px 12px;
    border-radius: 6px;
  }
  .dt-ext-link:hover {
    color: #f8fafc !important;
    background: #181c24;
  }

  /* KPI Grid */
  .dt-kpi-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
    margin: 14px 0 22px 0;
  }
  @media (max-width: 820px) {
    .dt-kpi-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
  .dt-card {
    background: #13161c;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 10px;
    padding: 16px 18px;
  }
  .dt-micro-label {
    font-size: 0.71rem;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #8b95a5;
    margin-bottom: 6px;
  }
  .dt-kpi-val {
    font-size: 1.32rem;
    font-weight: 600;
    color: #f8fafc;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
  }
  .dt-kpi-sub {
    font-size: 0.78rem;
    color: #94a3b8;
    margin-top: 4px;
    font-variant-numeric: tabular-nums;
  }

  /* Semantic financial colors ONLY */
  .pnl-pos { color: #22c55e !important; }
  .pnl-neg { color: #ef4444 !important; }
  .pnl-flat { color: #f8fafc !important; }

  /* Section Headings */
  .dt-section-title {
    font-size: 0.92rem;
    font-weight: 600;
    color: #f8fafc;
    margin: 22px 0 10px 0;
    letter-spacing: -0.01em;
  }

  /* Open Trade Grid */
  .dt-trade-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 14px;
    padding-bottom: 14px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }
  @media (max-width: 820px) {
    .dt-trade-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
  .dt-trade-val {
    font-size: 1.02rem;
    font-weight: 600;
    color: #f8fafc;
    font-variant-numeric: tabular-nums;
  }
  .dt-trade-note {
    font-size: 0.84rem;
    color: #cbd5e1;
    margin-top: 12px;
    line-height: 1.5;
  }

  /* Takeaway Banner */
  .dt-takeaway {
    background: #13161c;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 0.86rem;
    color: #cbd5e1;
    margin: 10px 0 16px 0;
    line-height: 1.45;
  }

  /* Clean Matte Tables */
  .dt-table-wrap {
    background: #13161c;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 10px;
    overflow-x: auto;
  }
  table.dt-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.83rem;
    font-variant-numeric: tabular-nums;
  }
  table.dt-table th {
    text-align: left;
    font-size: 0.70rem;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #8b95a5;
    padding: 11px 14px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.07);
    white-space: nowrap;
  }
  table.dt-table td {
    padding: 11px 14px;
    color: #f1f5f9;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    vertical-align: top;
  }
  table.dt-table tr:last-child td {
    border-bottom: none;
  }
  table.dt-table tr:hover td {
    background: #181c24;
  }
  .dt-muted-cell {
    color: #94a3b8 !important;
  }
</style>
"""


def _load_bot_config() -> BotConfig:
    """Resolve configuration from environment variables or Streamlit Secrets."""
    try:
        secrets_obj = st.secrets
        for key in (
            "ALPACA_API_KEY",
            "ALPACA_SECRET_KEY",
            "ALPACA_DATA_FEED",
            "STRATEGY_MODE",
            "ATR_PROFILE",
            "TRADING_SYMBOL",
        ):
            if not os.getenv(key) and key in secrets_obj:
                os.environ[key] = str(secrets_obj[key])
    except Exception:
        pass

    return BotConfig.from_env(env_file=REPO_ROOT / ".env")


def _pnl_class(val: float) -> str:
    if val > 0.004:
        return "pnl-pos"
    if val < -0.004:
        return "pnl-neg"
    return "pnl-flat"


def _fmt_signed_usd(val: float) -> str:
    if val > 0.004:
        return f"+${val:,.2f}"
    if val < -0.004:
        return f"-${abs(val):,.2f}"
    return "$0.00"


def _fmt_signed_pct(val: float) -> str:
    if val > 0.004:
        return f"+{val:.2f}%"
    if val < -0.004:
        return f"-{abs(val):.2f}%"
    return "0.00%"


def _render_header(snap: DashboardSnapshot) -> None:
    dot_cls = "dt-dot dt-dot-live" if snap.market_open else "dt-dot"
    esc_status = html.escape(snap.header_status_line)
    esc_sym = html.escape(snap.symbol)
    header_html = (
        '<div class="dt-header">'
        "<div>"
        f'<div class="dt-title">{esc_sym} Day Trader · Paper Account</div>'
        f'<div class="dt-subtitle"><span class="{dot_cls}"></span><span>{esc_status}</span></div>'
        "</div>"
        f'<a class="dt-ext-link" href="{ALPACA_PAPER_PORTAL_URL}" target="_blank" rel="noopener">'
        "Alpaca Paper Portal &#8599;"
        "</a>"
        "</div>"
    )
    st.markdown(header_html, unsafe_allow_html=True)


def _render_kpi_strip(snap: DashboardSnapshot) -> None:
    today_cls = _pnl_class(snap.today_pnl_usd)
    today_usd_str = _fmt_signed_usd(snap.today_pnl_usd)
    today_pct_str = _fmt_signed_pct(snap.today_pnl_pct)

    net30_cls = _pnl_class(snap.net_pnl_30d_usd)
    net30_str = _fmt_signed_usd(snap.net_pnl_30d_usd)
    if snap.closed_trades_30d > 0 and snap.win_rate_30d_pct is not None:
        trades_sub = f"{snap.closed_trades_30d} closed · {snap.win_rate_30d_pct:.0f}% win rate"
    else:
        trades_sub = "No closed trades in last 30 days"

    close_sgt = format_eod_close_sgt()
    kpi_html = (
        '<div class="dt-kpi-grid">'
        '<div class="dt-card">'
        '<div class="dt-micro-label">Paper Account Equity</div>'
        f'<div class="dt-kpi-val">${snap.equity_usd:,.2f}</div>'
        f'<div class="dt-kpi-sub">Cash ${snap.cash_usd:,.2f} · Buying Power ${snap.buying_power_usd:,.0f}</div>'
        "</div>"
        '<div class="dt-card">'
        '<div class="dt-micro-label">Today&#39;s P&amp;L</div>'
        f'<div class="dt-kpi-val {today_cls}">{today_usd_str}</div>'
        f'<div class="dt-kpi-sub {today_cls}">{today_pct_str} today</div>'
        "</div>"
        '<div class="dt-card">'
        '<div class="dt-micro-label">30-Day Realized P&amp;L</div>'
        f'<div class="dt-kpi-val {net30_cls}">{net30_str}</div>'
        f'<div class="dt-kpi-sub">{html.escape(trades_sub)}</div>'
        "</div>"
        '<div class="dt-card">'
        '<div class="dt-micro-label">Automatic Protection</div>'
        '<div class="dt-kpi-val">1.0% Max Risk</div>'
        f'<div class="dt-kpi-sub">All trades close by {html.escape(close_sgt)}</div>'
        "</div>"
        "</div>"
    )
    st.markdown(kpi_html, unsafe_allow_html=True)


def _render_open_trade_section(
    open_trade: OpenTradeView | None,
    working_entry: WorkingEntryView | None,
    cash_usd: float,
) -> None:
    st.markdown('<div class="dt-section-title">Open Trade</div>', unsafe_allow_html=True)

    if open_trade is not None:
        u_cls = _pnl_class(open_trade.unrealized_pnl_usd)
        u_usd = _fmt_signed_usd(open_trade.unrealized_pnl_usd)
        u_pct = _fmt_signed_pct(open_trade.unrealized_pnl_pct)

        if open_trade.stop_loss_price is not None and open_trade.max_loss_usd is not None:
            sl_str = f"${open_trade.stop_loss_price:,.2f}"
            sl_sub = f"-${open_trade.max_loss_usd:,.2f} max loss"
        else:
            sl_str = "Pending"
            sl_sub = "Updating from broker"

        if open_trade.profit_target_price is not None and open_trade.target_gain_usd is not None:
            tp_str = f"${open_trade.profit_target_price:,.2f}"
            tp_sub = f"+${open_trade.target_gain_usd:,.2f} target gain"
        else:
            tp_str = "Pending"
            tp_sub = "Updating from broker"

        esc_dir = html.escape(open_trade.direction_label)
        esc_sym = html.escape(open_trade.symbol)
        esc_sum = html.escape(open_trade.plain_summary)
        esc_close = html.escape(open_trade.mandatory_close_label)

        card_html = (
            '<div class="dt-card">'
            '<div class="dt-trade-grid">'
            f'<div><div class="dt-micro-label">{esc_sym} · {esc_dir}</div>'
            f'<div class="dt-trade-val">{open_trade.shares} shares</div>'
            f'<div class="dt-kpi-sub">${open_trade.market_value_usd:,.2f} value</div></div>'
            '<div><div class="dt-micro-label">Entry / Current Price</div>'
            f'<div class="dt-trade-val">${open_trade.entry_price:,.2f} &#8594; ${open_trade.current_price:,.2f}</div>'
            f'<div class="dt-kpi-sub">Auto-closes at {esc_close}</div></div>'
            '<div><div class="dt-micro-label">Unrealized P&amp;L</div>'
            f'<div class="dt-trade-val {u_cls}">{u_usd}</div>'
            f'<div class="dt-kpi-sub {u_cls}">{u_pct}</div></div>'
            '<div><div class="dt-micro-label">Auto Stop-Loss / Profit Target</div>'
            f'<div class="dt-trade-val">{sl_str} / {tp_str}</div>'
            f'<div class="dt-kpi-sub">{sl_sub} · {tp_sub}</div></div>'
            "</div>"
            f'<div class="dt-trade-note">{esc_sum}</div>'
            "</div>"
        )
        st.markdown(card_html, unsafe_allow_html=True)
        return

    if working_entry is not None:
        sl_txt = (
            f"${working_entry.stop_loss_price:,.2f}"
            if working_entry.stop_loss_price is not None
            else "—"
        )
        tp_txt = (
            f"${working_entry.profit_target_price:,.2f}"
            if working_entry.profit_target_price is not None
            else "—"
        )
        w_html = (
            '<div class="dt-card">'
            '<div class="dt-trade-grid">'
            f'<div><div class="dt-micro-label">Working Order</div>'
            f'<div class="dt-trade-val">{html.escape(working_entry.symbol)} · {html.escape(working_entry.action_label)}</div>'
            f'<div class="dt-kpi-sub">Submitted {html.escape(working_entry.submitted_sgt)}</div></div>'
            '<div><div class="dt-micro-label">Quantity</div>'
            f'<div class="dt-trade-val">{working_entry.shares} shares</div></div>'
            '<div><div class="dt-micro-label">Entry Collar</div>'
            f'<div class="dt-trade-val">{html.escape(working_entry.limit_or_market_label)}</div></div>'
            '<div><div class="dt-micro-label">Attached Stop / Target</div>'
            f'<div class="dt-trade-val">{sl_txt} / {tp_txt}</div></div>'
            "</div>"
            f'<div class="dt-trade-note">{html.escape(working_entry.plain_summary)}</div>'
            "</div>"
        )
        st.markdown(w_html, unsafe_allow_html=True)
        return

    flat_html = (
        '<div class="dt-card">'
        '<div class="dt-trade-val">No Open Position</div>'
        f'<div class="dt-trade-note" style="margin-top:6px;">'
        f"All ${cash_usd:,.2f} of cash is safely on the sidelines. The bot checks closed 5-minute bars every minute "
        f"during US market hours and only enters when trend, price support, and momentum align."
        "</div>"
        "</div>"
    )
    st.markdown(flat_html, unsafe_allow_html=True)


def _render_orders_table(rows: list[OrderJournalRow], empty_message: str) -> None:
    if not rows:
        empty_html = (
            '<div class="dt-card">'
            f'<div class="dt-kpi-sub" style="margin:0;">{html.escape(empty_message)}</div>'
            "</div>"
        )
        st.markdown(empty_html, unsafe_allow_html=True)
        return

    body_parts: list[str] = []
    for r in rows:
        if r.realized_pnl_usd is None:
            pnl_cell = '<td class="dt-muted-cell">—</td>'
        else:
            p_cls = _pnl_class(r.realized_pnl_usd)
            p_txt = _fmt_signed_usd(r.realized_pnl_usd)
            pnl_cell = f'<td class="{p_cls}" style="font-weight:600;">{p_txt}</td>'

        row_html = (
            "<tr>"
            f'<td style="white-space:nowrap;">{html.escape(r.timestamp_sgt_label)}</td>'
            f'<td style="font-weight:600;">{html.escape(r.symbol)}</td>'
            f"<td>{html.escape(r.action_label)}</td>"
            f"<td>{r.shares:,}</td>"
            f'<td style="white-space:nowrap;">{html.escape(r.price_label)}</td>'
            f"<td>{html.escape(r.status_label)}</td>"
            f"{pnl_cell}"
            f'<td class="dt-muted-cell">{html.escape(r.plain_summary)}</td>'
            "</tr>"
        )
        body_parts.append(row_html)

    joined_rows = "".join(body_parts)
    table_html = (
        '<div class="dt-table-wrap">'
        '<table class="dt-table">'
        "<thead><tr>"
        "<th>Date / Time (SGT)</th>"
        "<th>Symbol</th>"
        "<th>Action</th>"
        "<th>Shares</th>"
        "<th>Price ($)</th>"
        "<th>Status</th>"
        "<th>Realized P&amp;L</th>"
        "<th>Summary</th>"
        "</tr></thead>"
        f"<tbody>{joined_rows}</tbody>"
        "</table></div>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def _render_daily_pnl_table(rows: list[DailyPnLRow]) -> None:
    body_parts: list[str] = []
    for r in rows:
        p_cls = _pnl_class(r.day_pnl_usd)
        p_usd = _fmt_signed_usd(r.day_pnl_usd)
        p_pct = _fmt_signed_pct(r.day_return_pct)
        wl_txt = f"{r.wins}W / {r.losses}L" if r.trades_closed > 0 else "—"

        row_html = (
            "<tr>"
            f'<td style="font-weight:500;">{html.escape(r.date_label)}</td>'
            f"<td>{r.trades_closed}</td>"
            f'<td class="dt-muted-cell">{wl_txt}</td>'
            f'<td class="{p_cls}" style="font-weight:600;">{p_usd}</td>'
            f'<td class="{p_cls}">{p_pct}</td>'
            f"<td>${r.ending_equity_usd:,.2f}</td>"
            "</tr>"
        )
        body_parts.append(row_html)

    joined_rows = "".join(body_parts)
    table_html = (
        '<div class="dt-table-wrap">'
        '<table class="dt-table">'
        "<thead><tr>"
        "<th>Trading Date</th>"
        "<th>Trades Closed</th>"
        "<th>Wins / Losses</th>"
        "<th>Day P&amp;L ($)</th>"
        "<th>Day Return (%)</th>"
        "<th>Ending Equity ($)</th>"
        "</tr></thead>"
        f"<tbody>{joined_rows}</tbody>"
        "</table></div>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def _day_option_label(d: date, count: int, is_latest: bool) -> str:
    ord_word = "order" if count == 1 else "orders"
    suffix = " · Latest" if is_latest else ""
    return f"{d.strftime('%a, %b %d')} · {count} {ord_word}{suffix}"


@st.fragment(run_every="30s")
def _render_live_dashboard() -> None:
    try:
        cfg = _load_bot_config()
        snap = fetch_dashboard_snapshot(cfg)
    except Exception as exc:
        err_msg = html.escape(str(exc))
        st.markdown(
            '<div class="dt-card">'
            '<div class="dt-trade-val">Connection Configuration Needed</div>'
            f'<div class="dt-trade-note">{err_msg}</div>'
            "</div>",
            unsafe_allow_html=True,
        )
        return

    _render_header(snap)

    tab_today, tab_journal = st.tabs(["Today & Open Trade", "Daily P&L & Journal"])

    with tab_today:
        _render_kpi_strip(snap)
        _render_open_trade_section(snap.open_trade, snap.working_entry, snap.cash_usd)

        latest_day = snap.available_days[0]
        today_orders = snap.orders_by_day.get(latest_day, [])
        st.markdown(
            f'<div class="dt-section-title">Latest Session Orders &amp; Fills ({latest_day.strftime("%b %d, %Y")})</div>',
            unsafe_allow_html=True,
        )
        _render_orders_table(
            today_orders,
            "No orders have been placed in the current session yet.",
        )

    with tab_journal:
        st.markdown(
            '<div class="dt-section-title" style="margin-top:12px;">Daily P&amp;L Summary</div>',
            unsafe_allow_html=True,
        )
        _render_daily_pnl_table(snap.daily_pnl_rows)

        st.markdown(
            '<div class="dt-section-title">Single-Day Trade Journal</div>',
            unsafe_allow_html=True,
        )

        days = snap.available_days
        if "journal_day_idx" not in st.session_state:
            st.session_state.journal_day_idx = 0
        if st.session_state.journal_day_idx >= len(days):
            st.session_state.journal_day_idx = 0

        col_prev, col_sel, col_next = st.columns([1.2, 5.6, 1.2])
        with col_prev:
            if st.button(
                "← Previous",
                disabled=(st.session_state.journal_day_idx >= len(days) - 1),
                use_container_width=True,
            ):
                st.session_state.journal_day_idx += 1
                st.rerun()
        with col_next:
            if st.button(
                "Next →",
                disabled=(st.session_state.journal_day_idx <= 0),
                use_container_width=True,
            ):
                st.session_state.journal_day_idx -= 1
                st.rerun()
        with col_sel:
            labels = [
                _day_option_label(d, len(snap.orders_by_day.get(d, [])), idx == 0)
                for idx, d in enumerate(days)
            ]
            chosen_label = st.selectbox(
                "Select Trading Day",
                options=labels,
                index=st.session_state.journal_day_idx,
                label_visibility="collapsed",
            )
            chosen_idx = labels.index(chosen_label)
            if chosen_idx != st.session_state.journal_day_idx:
                st.session_state.journal_day_idx = chosen_idx
                st.rerun()

        selected_day = days[st.session_state.journal_day_idx]
        day_orders = snap.orders_by_day.get(selected_day, [])
        pnl_lookup = {r.session_date: r.day_pnl_usd for r in snap.daily_pnl_rows}
        takeaway = build_daily_takeaway(
            session_date=selected_day,
            orders=day_orders,
            day_pnl_usd=pnl_lookup.get(selected_day),
            open_trade=snap.open_trade,
            is_latest_day=(st.session_state.journal_day_idx == 0),
        )
        st.markdown(
            f'<div class="dt-takeaway">{html.escape(takeaway)}</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            '<div class="dt-micro-label" style="margin: 8px 0;">New Orders &amp; Fills</div>',
            unsafe_allow_html=True,
        )
        _render_orders_table(
            day_orders,
            f"No orders or fills recorded on {selected_day.strftime('%b %d, %Y')}.",
        )

        day_skips = snap.skipped_by_day.get(selected_day, [])
        if day_skips:
            with st.expander(f"Skipped Bar Checks ({len(day_skips)} deduplicated)"):
                skip_parts: list[str] = []
                for sk in day_skips:
                    skip_parts.append(
                        "<tr>"
                        f'<td style="white-space:nowrap;">{html.escape(sk.timestamp_sgt_label)}</td>'
                        f"<td>{html.escape(sk.symbol)}</td>"
                        f'<td class="dt-muted-cell">{html.escape(sk.plain_reason)}</td>'
                        "</tr>"
                    )
                skip_rows_html = "".join(skip_parts)
                st.markdown(
                    '<div class="dt-table-wrap"><table class="dt-table">'
                    "<thead><tr><th>Date / Time (SGT)</th><th>Symbol</th><th>Reason Skipped</th></tr></thead>"
                    f"<tbody>{skip_rows_html}</tbody></table></div>",
                    unsafe_allow_html=True,
                )


def main() -> None:
    st.set_page_config(
        page_title="SPY Day Trader · Paper Account",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(MINIMAL_DARK_CSS, unsafe_allow_html=True)
    _render_live_dashboard()


if __name__ == "__main__":
    main()

