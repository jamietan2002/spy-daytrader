"""Beginner-friendly plain-English telemetry, SGT timestamps, and daily P&L reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest

from spy_daytrader.config import BotConfig
from spy_daytrader.state_store import BOT_ORDER_PREFIX, BotHeartbeat, SQLiteStateLedger

SGT_TZ = ZoneInfo("Asia/Singapore")
NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class OpenTradeView:
    """Plain-English snapshot of an active open position and its automatic exit orders."""

    symbol: str
    direction_label: str  # "Long (Bought)" or "Short (Sold Short)"
    shares: int
    entry_price: float
    current_price: float
    market_value_usd: float
    unrealized_pnl_usd: float
    unrealized_pnl_pct: float
    stop_loss_price: float | None
    max_loss_usd: float | None
    profit_target_price: float | None
    target_gain_usd: float | None
    mandatory_close_label: str
    plain_summary: str


@dataclass(frozen=True)
class WorkingEntryView:
    """Plain-English snapshot of an unfilled entry order waiting at the broker."""

    symbol: str
    action_label: str
    shares: int
    limit_or_market_label: str
    stop_loss_price: float | None
    profit_target_price: float | None
    submitted_sgt: str
    plain_summary: str


@dataclass(frozen=True)
class OrderJournalRow:
    """Single human-readable order/fill row sorted newest-first in SGT."""

    timestamp_utc: datetime
    timestamp_sgt_label: str
    session_date: date
    symbol: str
    action_label: str
    shares: int
    price_label: str
    status_label: str
    realized_pnl_usd: float | None
    plain_summary: str
    is_bot_order: bool


@dataclass(frozen=True)
class DailyPnLRow:
    """Single trading session summary for the Daily P&L History table."""

    session_date: date
    date_label: str
    trades_closed: int
    wins: int
    losses: int
    day_pnl_usd: float
    day_return_pct: float
    ending_equity_usd: float


@dataclass(frozen=True)
class SkippedCheckRow:
    """Deduplicated plain-English record of a skipped bar check."""

    timestamp_sgt_label: str
    session_date: date
    symbol: str
    plain_reason: str


@dataclass(frozen=True)
class DashboardSnapshot:
    """Complete view-model rendered by the 2-Tab Minimalist Dark UI."""

    symbol: str
    equity_usd: float
    cash_usd: float
    buying_power_usd: float
    today_pnl_usd: float
    today_pnl_pct: float
    market_open: bool
    header_status_line: str
    closed_trades_30d: int
    win_rate_30d_pct: float | None
    net_pnl_30d_usd: float
    open_trade: OpenTradeView | None
    working_entry: WorkingEntryView | None
    orders_by_day: dict[date, list[OrderJournalRow]]
    skipped_by_day: dict[date, list[SkippedCheckRow]]
    daily_pnl_rows: list[DailyPnLRow]
    available_days: list[date]


def format_sgt(dt: datetime | None) -> str:
    """Format any datetime into calm, explicit Singapore Time (`Nov 14, 20:15 SGT`)."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    sgt = dt.astimezone(SGT_TZ)
    return f"{sgt.strftime('%b %d, %H:%M')} SGT"


def format_eod_close_sgt(reference_utc: datetime | None = None) -> str:
    """Convert 15:55 America/New_York into current DST-aware SGT time (03:55 or 04:55 SGT)."""
    ref = reference_utc or datetime.now(timezone.utc)
    ny_now = ref.astimezone(NY_TZ)
    flatten_ny = ny_now.replace(hour=15, minute=55, second=0, microsecond=0)
    flatten_sgt = flatten_ny.astimezone(SGT_TZ)
    return f"{flatten_sgt.strftime('%H:%M')} SGT"


def translate_skip_reason(raw_reason: str) -> str:
    """Translate internal strategy reason codes into calm, zero-jargon plain English."""
    code = (raw_reason or "").upper()
    if "OUTSIDE_ENTRY_WINDOW" in code:
        return "Outside scheduled trading window; waiting for active session hours."
    if "LUNCH_CHOP" in code:
        return "Skipped midday low-volume period (11:00–13:00 US Eastern) to avoid choppy price swings."
    if "MORNING_SQUEEZE" in code:
        return "Skipped early-morning short entry while opening buying pressure was still active."
    if "BELOW_VWAP" in code:
        return "Price remained below today's volume-weighted average price; waited for stronger support."
    if "EMA50_BELOW_EMA200" in code or "DECLINING_EMA" in code:
        return "Broader intraday trend was not yet aligned upward; skipped dip entry."
    if "NON_BULLISH_CANDLE" in code:
        return "Dip had not yet turned upward on a positive 5-minute bar; waited for confirmation."
    if "NON_DECLINING_EMA" in code:
        return "Broader intraday trend was still rising; skipped short entry."
    if "LOW_VOLATILITY" in code:
        return "Market movement was too quiet to justify a day trade."
    if "EXTREME_VOLATILITY" in code:
        return "Skipped unusually sharp price spike to avoid wide fill slippage."
    if "CIRCUIT_BREAKER" in code:
        return "Daily loss limit reached; new entries paused for the rest of today's session."
    if "NO_SETUP" in code:
        return "Checked latest closed 5-minute bar; price and momentum did not line up for an entry."
    if "INSUFFICIENT_BARS" in code or "WARMUP" in code:
        return "Loading recent market history before evaluating setups."
    return "Checked market conditions; no trade required on this bar."


def translate_order_action(
    *,
    side: str,
    is_entry: bool,
    order_type: str,
    is_eod_close: bool = False,
) -> str:
    """Return beginner-clear order action labels without broker jargon."""
    s = side.upper()
    ot = order_type.upper()
    if is_eod_close:
        return "End-of-Day Close (Sell)" if s == "SELL" else "End-of-Day Close (Buy to Cover)"
    if is_entry:
        return "Entry (Buy)" if "BUY" in s else "Entry (Short)"
    if "STOP" in ot:
        return "Auto Stop-Loss (Sell)" if "SELL" in s else "Auto Stop-Loss (Buy to Cover)"
    if "LIMIT" in ot:
        return "Auto Profit Target (Sell)" if "SELL" in s else "Auto Profit Target (Buy to Cover)"
    return "Auto Exit (Sell)" if "SELL" in s else "Auto Exit (Buy to Cover)"


def translate_order_status(raw_status: str, client_order_id: str = "") -> str:
    """Translate Alpaca order status enums into clean everyday English."""
    st = str(raw_status or "").lower()
    if "." in st:
        st = st.split(".")[-1]
    if st == "filled":
        return "Filled"
    if st == "partially_filled":
        return "Partially Filled"
    if st in {"new", "accepted", "pending_new", "accepted_for_bidding"}:
        return "Working at Broker"
    if st == "held":
        return "Armed (Waiting for Trigger)"
    if st in {"canceled", "cancelled", "cancelled_stale"}:
        if client_order_id.startswith(BOT_ORDER_PREFIX):
            return "Cancelled (Unfilled)"
        return "Cancelled (Replaced by Other Exit)"
    if st == "expired":
        return "Expired at Market Close"
    if st == "rejected":
        return "Rejected by Broker"
    return "Submitted"


def build_header_status(
    *,
    market_open: bool,
    next_open_utc: datetime | None,
    heartbeat: BotHeartbeat | None,
    reference_utc: datetime | None = None,
) -> str:
    """Build a 1-line calm header status in SGT without emojis or technical codes."""
    ref = reference_utc or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    hb_is_fresh = False
    if heartbeat is not None:
        hb_ts = heartbeat.updated_at_utc
        if hb_ts.tzinfo is None:
            hb_ts = hb_ts.replace(tzinfo=timezone.utc)
        hb_is_fresh = abs((ref - hb_ts).total_seconds()) <= 600.0

    if market_open:
        if heartbeat is not None and hb_is_fresh:
            return f"US Market Open · Autonomous bot active · Last checked {format_sgt(heartbeat.updated_at_utc)}"
        return "US Market Open · Autonomous bot active · Connected to Alpaca Paper"

    next_open_str = format_sgt(next_open_utc)
    if heartbeat is not None and hb_is_fresh:
        return f"US Market Closed · Next session opens {next_open_str} · Bot standing by (checked {format_sgt(heartbeat.updated_at_utc)})"
    return f"US Market Closed · Next session opens {next_open_str}"


def build_daily_takeaway(
    *,
    session_date: date,
    orders: list[OrderJournalRow],
    day_pnl_usd: float | None,
    open_trade: OpenTradeView | None,
    is_latest_day: bool,
) -> str:
    """Compose a 1-sentence calm plain-English takeaway summarizing the selected day."""
    filled_entries = [o for o in orders if o.status_label == "Filled" and o.action_label.startswith("Entry")]
    filled_exits = [o for o in orders if o.status_label == "Filled" and not o.action_label.startswith("Entry")]
    cancelled_entries = [
        o for o in orders if o.status_label == "Cancelled (Unfilled)" and o.action_label.startswith("Entry")
    ]

    if not orders:
        if is_latest_day and open_trade is not None:
            return (
                f"Holding an open {open_trade.symbol} position of {open_trade.shares} shares with automatic "
                f"stop-loss and profit-target protection active at the broker."
            )
        return (
            f"No trades were placed on {session_date.strftime('%b %d, %Y')}; the bot stayed safely in cash "
            f"because market conditions did not line up for a high-probability entry."
        )

    pnl_phrase = ""
    if day_pnl_usd is not None and (filled_exits or abs(day_pnl_usd) >= 0.01):
        sign = "+" if day_pnl_usd >= 0 else "-"
        pnl_phrase = f" for a net day result of {sign}${abs(day_pnl_usd):,.2f}"

    parts: list[str] = []
    if filled_entries:
        n_ent = len(filled_entries)
        trade_word = "trade" if n_ent == 1 else "trades"
        parts.append(f"entered {n_ent} intraday {trade_word}")
    if filled_exits:
        n_ex = len(filled_exits)
        exit_word = "position" if n_ex == 1 else "positions"
        parts.append(f"closed {n_ex} {exit_word}{pnl_phrase}")
    if cancelled_entries and not filled_entries:
        n_can = len(cancelled_entries)
        ord_word = "order" if n_can == 1 else "orders"
        parts.append(f"cancelled {n_can} unfilled limit entry {ord_word} after price moved away")

    if not parts:
        return f"Recorded {len(orders)} broker order update(s) on {session_date.strftime('%b %d, %Y')}."

    joined = " and ".join(parts)
    return f"On {session_date.strftime('%b %d, %Y')}, the bot {joined}."


def fetch_dashboard_snapshot(
    config: BotConfig,
    *,
    trading_client: TradingClient | None = None,
    ledger: SQLiteStateLedger | None = None,
) -> DashboardSnapshot:
    """
    Query live Alpaca Paper account state + nested order history + local SQLite heartbeat
    and assemble a complete, plain-English `DashboardSnapshot`.
    """
    state_ledger = ledger or SQLiteStateLedger(config.state_db_path)
    if trading_client is not None:
        client = trading_client
    else:
        config.require_credentials()
        client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=True,
            url_override=config.alpaca_base_url,
        )

    acct = client.get_account()
    clock = client.get_clock()
    positions = list(client.get_all_positions())
    open_orders = list(
        client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100, nested=True)
        )
    )
    all_orders = list(
        client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=300, nested=True)
        )
    )

    equity = float(getattr(acct, "equity", 0.0) or 0.0)
    last_equity = float(getattr(acct, "last_equity", equity) or equity)
    cash = float(getattr(acct, "cash", 0.0) or 0.0)
    bp = float(getattr(acct, "buying_power", 0.0) or 0.0)
    today_pnl_usd = equity - last_equity
    today_pnl_pct = (today_pnl_usd / last_equity * 100.0) if last_equity > 0 else 0.0

    is_open = bool(getattr(clock, "is_open", False))
    next_open_utc = getattr(clock, "next_open", None)
    now_utc = getattr(clock, "timestamp", None) or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    today_ny = now_utc.astimezone(NY_TZ).date()

    hb = state_ledger.get_latest_heartbeat()
    header_line = build_header_status(
        market_open=is_open,
        next_open_utc=next_open_utc,
        heartbeat=hb,
        reference_utc=now_utc,
    )

    # 1. Active Open Position & Child Exit Legs
    open_trade = _extract_open_trade_view(
        symbol=config.symbol,
        positions=positions,
        open_orders=open_orders,
        now_utc=now_utc,
    )
    working_entry = (
        None
        if open_trade is not None
        else _extract_working_entry_view(symbol=config.symbol, open_orders=open_orders)
    )

    # 2. Reconstruct Order Journal & Round-Trip Realized P&L from Nested Broker Orders
    journal_rows, closed_roundtrips = _build_order_journal_and_roundtrips(all_orders)
    orders_by_day: dict[date, list[OrderJournalRow]] = {}
    for row in journal_rows:
        orders_by_day.setdefault(row.session_date, []).append(row)
    for d_key in orders_by_day:
        orders_by_day[d_key].sort(key=lambda r: r.timestamp_utc, reverse=True)

    # 3. Skipped checks from SQLite (deduplicated per day + plain-English reason)
    skipped_by_day: dict[date, list[SkippedCheckRow]] = {}
    seen_skip_keys: set[tuple[date, str, str]] = set()
    for rec in state_ledger.list_recent_processed_bars(limit=150):
        act = str(rec.get("action", "")).upper()
        rsn = str(rec.get("reason", ""))
        if act != "NONE" and not rsn.startswith("RISK_VETO"):
            continue
        ts_raw = rec.get("created_at_utc", "")
        try:
            dt_utc = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        sess_d = dt_utc.astimezone(NY_TZ).date()
        sym = str(rec.get("symbol", config.symbol)).upper()
        plain_r = translate_skip_reason(rsn)
        dedup_key = (sess_d, sym, plain_r)
        if dedup_key in seen_skip_keys:
            continue
        seen_skip_keys.add(dedup_key)
        skipped_by_day.setdefault(sess_d, []).append(
            SkippedCheckRow(
                timestamp_sgt_label=format_sgt(dt_utc),
                session_date=sess_d,
                symbol=sym,
                plain_reason=plain_r,
            )
        )

    # 4. Daily P&L Table (merging Alpaca PortfolioHistory with closed round-trip counts)
    daily_pnl_rows = _build_daily_pnl_rows(
        client=client,
        closed_roundtrips=closed_roundtrips,
        orders_by_day=orders_by_day,
        today_ny=today_ny,
        current_equity=equity,
        today_pnl_usd=today_pnl_usd,
        today_pnl_pct=today_pnl_pct,
    )

    # 30-day closed trade stats
    closed_cnt = len(closed_roundtrips)
    wins_cnt = sum(1 for rt in closed_roundtrips if rt["pnl_usd"] > 0)
    win_rate_30d = (wins_cnt / closed_cnt * 100.0) if closed_cnt > 0 else None
    net_30d = sum(float(rt["pnl_usd"]) for rt in closed_roundtrips)

    all_day_keys = set(orders_by_day.keys()) | {r.session_date for r in daily_pnl_rows} | {today_ny}
    available_days = sorted(all_day_keys, reverse=True)

    return DashboardSnapshot(
        symbol=config.symbol,
        equity_usd=round(equity, 2),
        cash_usd=round(cash, 2),
        buying_power_usd=round(bp, 2),
        today_pnl_usd=round(today_pnl_usd, 2),
        today_pnl_pct=round(today_pnl_pct, 2),
        market_open=is_open,
        header_status_line=header_line,
        closed_trades_30d=closed_cnt,
        win_rate_30d_pct=round(win_rate_30d, 1) if win_rate_30d is not None else None,
        net_pnl_30d_usd=round(net_30d, 2),
        open_trade=open_trade,
        working_entry=working_entry,
        orders_by_day=orders_by_day,
        skipped_by_day=skipped_by_day,
        daily_pnl_rows=daily_pnl_rows,
        available_days=available_days,
    )


def _extract_open_trade_view(
    *,
    symbol: str,
    positions: list[Any],
    open_orders: list[Any],
    now_utc: datetime,
) -> OpenTradeView | None:
    pos = next(
        (p for p in positions if str(getattr(p, "symbol", "")).upper() == symbol.upper()),
        None,
    )
    if pos is None:
        return None

    raw_qty = float(getattr(pos, "qty", 0.0) or 0.0)
    if raw_qty == 0.0:
        return None

    is_long = raw_qty > 0
    shares = abs(int(round(raw_qty)))
    entry_px = float(getattr(pos, "avg_entry_price", 0.0) or 0.0)
    curr_px = float(getattr(pos, "current_price", entry_px) or entry_px)
    mkt_val = abs(float(getattr(pos, "market_value", shares * curr_px) or (shares * curr_px)))
    unreal_usd = float(getattr(pos, "unrealized_pl", 0.0) or 0.0)
    unreal_pct = float(getattr(pos, "unrealized_plpc", 0.0) or 0.0) * 100.0

    sl_px: float | None = None
    tp_px: float | None = None
    for o in _flatten_orders_with_legs(open_orders):
        if str(getattr(o, "symbol", "")).upper() != symbol.upper():
            continue
        sp = getattr(o, "stop_price", None)
        lp = getattr(o, "limit_price", None)
        if sp is not None and float(sp) > 0:
            sl_px = float(sp)
        elif lp is not None and float(lp) > 0:
            tp_px = float(lp)

    max_loss = (
        round(abs(entry_px - sl_px) * shares, 2) if sl_px is not None and entry_px > 0 else None
    )
    target_gain = (
        round(abs(tp_px - entry_px) * shares, 2) if tp_px is not None and entry_px > 0 else None
    )
    close_sgt = format_eod_close_sgt(now_utc)

    if is_long:
        direction = "Long (Bought)"
        summary = (
            f"Bought {shares} shares of {symbol} on a confirmed uptrend pullback; "
            f"automatic stop-loss and profit-target exits are active at the broker, "
            f"and any remaining shares close automatically at {close_sgt}."
        )
    else:
        direction = "Short (Sold Short)"
        summary = (
            f"Sold short {shares} shares of {symbol} into a confirmed intraday downtrend bounce; "
            f"automatic buy-to-cover stop-loss and profit-target exits are active at the broker, "
            f"and any remaining shares close automatically at {close_sgt}."
        )

    return OpenTradeView(
        symbol=symbol.upper(),
        direction_label=direction,
        shares=shares,
        entry_price=round(entry_px, 2),
        current_price=round(curr_px, 2),
        market_value_usd=round(mkt_val, 2),
        unrealized_pnl_usd=round(unreal_usd, 2),
        unrealized_pnl_pct=round(unreal_pct, 2),
        stop_loss_price=round(sl_px, 2) if sl_px is not None else None,
        max_loss_usd=max_loss,
        profit_target_price=round(tp_px, 2) if tp_px is not None else None,
        target_gain_usd=target_gain,
        mandatory_close_label=close_sgt,
        plain_summary=summary,
    )


def _extract_working_entry_view(
    *,
    symbol: str,
    open_orders: list[Any],
) -> WorkingEntryView | None:
    for o in open_orders:
        if str(getattr(o, "symbol", "")).upper() != symbol.upper():
            continue
        cid = str(getattr(o, "client_order_id", "") or "")
        side_str = str(getattr(o, "side", "buy")).lower()
        is_buy = "buy" in side_str
        qty = int(float(getattr(o, "qty", 0) or 0))
        lp = getattr(o, "limit_price", None)
        price_lbl = f"Limit ${float(lp):,.2f}" if lp is not None else "Market"

        sl_px: float | None = None
        tp_px: float | None = None
        for leg in getattr(o, "legs", None) or []:
            if getattr(leg, "stop_price", None) is not None:
                sl_px = float(leg.stop_price)
            elif getattr(leg, "limit_price", None) is not None:
                tp_px = float(leg.limit_price)

        sub_at = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
        return WorkingEntryView(
            symbol=symbol.upper(),
            action_label="Entry (Buy)" if is_buy else "Entry (Short)",
            shares=qty,
            limit_or_market_label=price_lbl,
            stop_loss_price=round(sl_px, 2) if sl_px is not None else None,
            profit_target_price=round(tp_px, 2) if tp_px is not None else None,
            submitted_sgt=format_sgt(sub_at),
            plain_summary=(
                "Waiting at the broker to fill within the protected price limit; "
                "cancels automatically within 1 minute if price moves away."
                if cid.startswith(BOT_ORDER_PREFIX) and lp is not None
                else "Working entry order submitted to Alpaca Paper."
            ),
        )
    return None


def _flatten_orders_with_legs(orders: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen_ids: set[str] = set()
    for o in orders:
        oid = str(getattr(o, "id", id(o)))
        if oid not in seen_ids:
            seen_ids.add(oid)
            out.append(o)
        for leg in getattr(o, "legs", None) or []:
            lid = str(getattr(leg, "id", id(leg)))
            if lid not in seen_ids:
                seen_ids.add(lid)
                out.append(leg)
    return out


def _build_order_journal_and_roundtrips(
    all_orders: list[Any],
) -> tuple[list[OrderJournalRow], list[dict[str, Any]]]:
    """
    Build human-readable `OrderJournalRow` items and match filled bracket/EOD exits
    against filled entries to compute exact realized dollar P&L per closed trade.
    """
    rows: list[OrderJournalRow] = []
    roundtrips: list[dict[str, Any]] = []

    # Track unmatched filled parent entries (FIFO per symbol) to pair standalone EOD market closes
    open_lots: dict[str, list[dict[str, Any]]] = {}

    # Process chronologically (oldest -> newest) so FIFO entry-to-exit matching is exact
    chronological_parents = sorted(
        all_orders,
        key=lambda x: getattr(x, "submitted_at", None)
        or getattr(x, "created_at", None)
        or datetime.min.replace(tzinfo=timezone.utc),
    )

    for o in chronological_parents:
        sym = str(getattr(o, "symbol", "SPY")).upper()
        cid = str(getattr(o, "client_order_id", "") or "")
        is_bot = cid.startswith(BOT_ORDER_PREFIX)
        side_raw = str(getattr(o, "side", "buy")).upper()
        if "." in side_raw:
            side_raw = side_raw.split(".")[-1]
        otype_raw = str(getattr(o, "type", getattr(o, "order_type", "market"))).upper()
        if "." in otype_raw:
            otype_raw = otype_raw.split(".")[-1]
        status_raw = str(getattr(o, "status", "")).lower()
        if "." in status_raw:
            status_raw = status_raw.split(".")[-1]

        legs = list(getattr(o, "legs", None) or [])
        is_bracket_parent = bool(legs) or is_bot

        ts = (
            getattr(o, "filled_at", None)
            or getattr(o, "submitted_at", None)
            or getattr(o, "created_at", None)
            or datetime.now(timezone.utc)
        )
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        sess_date = ts.astimezone(NY_TZ).date()

        qty = int(float(getattr(o, "filled_qty", 0) or getattr(o, "qty", 0) or 0))
        fill_px = getattr(o, "filled_avg_price", None)
        lim_px = getattr(o, "limit_price", None)
        stp_px = getattr(o, "stop_price", None)

        if fill_px is not None and float(fill_px) > 0:
            px_val = float(fill_px)
            px_label = f"${px_val:,.2f}"
        elif lim_px is not None and float(lim_px) > 0:
            px_val = float(lim_px)
            px_label = f"${px_val:,.2f} (Limit)"
        elif stp_px is not None and float(stp_px) > 0:
            px_val = float(stp_px)
            px_label = f"${px_val:,.2f} (Stop)"
        else:
            px_val = 0.0
            px_label = "Market"

        # Determine if this standalone non-bracket order closes an existing open lot (e.g. 15:55 EOD flatten)
        sym_lots = open_lots.setdefault(sym, [])
        closes_existing_lot = (
            not is_bracket_parent
            and bool(sym_lots)
            and (
                (sym_lots[0]["side"] == "BUY" and side_raw == "SELL")
                or (sym_lots[0]["side"] == "SELL" and side_raw == "BUY")
            )
        )

        is_entry = not closes_existing_lot
        action_lbl = translate_order_action(
            side=side_raw,
            is_entry=is_entry,
            order_type=otype_raw,
            is_eod_close=closes_existing_lot,
        )
        status_lbl = translate_order_status(status_raw, cid)

        realized_pnl: float | None = None
        if closes_existing_lot and status_lbl == "Filled" and px_val > 0 and sym_lots:
            lot = sym_lots.pop(0)
            entry_px = float(lot["entry_price"])
            matched_qty = min(qty, int(lot["qty"]))
            realized_pnl = round(
                (px_val - entry_px) * matched_qty
                if lot["side"] == "BUY"
                else (entry_px - px_val) * matched_qty,
                2,
            )
            roundtrips.append(
                {"session_date": sess_date, "symbol": sym, "pnl_usd": realized_pnl}
            )

        if is_entry and status_lbl == "Filled" and px_val > 0:
            # Check if one of its attached bracket child legs already filled
            filled_leg = next(
                (
                    lg
                    for lg in legs
                    if str(getattr(lg, "status", "")).lower().endswith("filled")
                    and getattr(lg, "filled_avg_price", None) is not None
                ),
                None,
            )
            if filled_leg is None:
                sym_lots.append({"side": side_raw, "qty": qty, "entry_price": px_val})

        summary = _describe_order_plainly(
            is_entry=is_entry,
            side=side_raw,
            status_label=status_lbl,
            is_eod_close=closes_existing_lot,
        )
        rows.append(
            OrderJournalRow(
                timestamp_utc=ts,
                timestamp_sgt_label=format_sgt(ts),
                session_date=sess_date,
                symbol=sym,
                action_label=action_lbl,
                shares=qty,
                price_label=px_label,
                status_label=status_lbl,
                realized_pnl_usd=realized_pnl,
                plain_summary=summary,
                is_bot_order=is_bot,
            )
        )

        # Render filled or active child exit legs (`TakeProfit` / `StopLoss`) cleanly
        for leg in legs:
            leg_status_raw = str(getattr(leg, "status", "")).lower()
            if "." in leg_status_raw:
                leg_status_raw = leg_status_raw.split(".")[-1]
            # Hide auto-cancelled sibling leg to keep journal noise-free (per SKILL.md Section 5.4)
            if leg_status_raw in {"canceled", "cancelled", "expired"}:
                continue

            leg_side = str(getattr(leg, "side", "sell")).upper().split(".")[-1]
            leg_type = str(getattr(leg, "type", getattr(leg, "order_type", "limit"))).upper().split(".")[-1]
            leg_ts = (
                getattr(leg, "filled_at", None)
                or getattr(leg, "submitted_at", None)
                or ts
            )
            if leg_ts.tzinfo is None:
                leg_ts = leg_ts.replace(tzinfo=timezone.utc)
            leg_date = leg_ts.astimezone(NY_TZ).date()
            leg_qty = int(float(getattr(leg, "filled_qty", 0) or getattr(leg, "qty", qty) or qty))
            leg_fill = getattr(leg, "filled_avg_price", None)
            leg_lim = getattr(leg, "limit_price", None)
            leg_stp = getattr(leg, "stop_price", None)

            if leg_fill is not None and float(leg_fill) > 0:
                l_px = float(leg_fill)
                l_px_lbl = f"${l_px:,.2f}"
            elif leg_stp is not None and float(leg_stp) > 0:
                l_px = float(leg_stp)
                l_px_lbl = f"${l_px:,.2f} (Stop)"
            elif leg_lim is not None and float(leg_lim) > 0:
                l_px = float(leg_lim)
                l_px_lbl = f"${l_px:,.2f} (Target)"
            else:
                l_px = 0.0
                l_px_lbl = "Market"

            leg_status_lbl = translate_order_status(leg_status_raw)
            leg_action_lbl = translate_order_action(
                side=leg_side,
                is_entry=False,
                order_type=leg_type,
            )

            leg_pnl: float | None = None
            if leg_status_lbl == "Filled" and px_val > 0 and l_px > 0:
                leg_pnl = round(
                    (l_px - px_val) * leg_qty
                    if side_raw == "BUY"
                    else (px_val - l_px) * leg_qty,
                    2,
                )
                roundtrips.append(
                    {"session_date": leg_date, "symbol": sym, "pnl_usd": leg_pnl}
                )

            rows.append(
                OrderJournalRow(
                    timestamp_utc=leg_ts,
                    timestamp_sgt_label=format_sgt(leg_ts),
                    session_date=leg_date,
                    symbol=sym,
                    action_label=leg_action_lbl,
                    shares=leg_qty,
                    price_label=l_px_lbl,
                    status_label=leg_status_lbl,
                    realized_pnl_usd=leg_pnl,
                    plain_summary=_describe_order_plainly(
                        is_entry=False,
                        side=leg_side,
                        status_label=leg_status_lbl,
                        is_stop="STOP" in leg_type,
                    ),
                    is_bot_order=is_bot,
                )
            )

    rows.sort(key=lambda r: r.timestamp_utc, reverse=True)
    return rows, roundtrips


def _describe_order_plainly(
    *,
    is_entry: bool,
    side: str,
    status_label: str,
    is_eod_close: bool = False,
    is_stop: bool = False,
) -> str:
    if is_eod_close:
        return "Closed remaining intraday shares before the US market close so nothing is held overnight."
    if is_entry:
        if status_label == "Cancelled (Unfilled)":
            return "Price moved away within 1 minute before filling; order cancelled safely without chasing."
        if "BUY" in side.upper():
            return "Entered long on an uptrend pullback with automatic stop-loss and profit-target protection."
        return "Entered short on a downtrend bounce with automatic stop-loss and profit-target protection."
    if is_stop:
        if status_label == "Filled":
            return "Automatic stop-loss triggered to cap downside within the 1% risk budget."
        return "Automatic stop-loss standing by at the broker to cap downside risk."
    if status_label == "Filled":
        return "Automatic profit target reached, locking in gains."
    return "Automatic profit target standing by at the broker."


def _build_daily_pnl_rows(
    *,
    client: TradingClient,
    closed_roundtrips: list[dict[str, Any]],
    orders_by_day: dict[date, list[OrderJournalRow]],
    today_ny: date,
    current_equity: float,
    today_pnl_usd: float,
    today_pnl_pct: float,
) -> list[DailyPnLRow]:
    rt_by_day: dict[date, list[float]] = {}
    for rt in closed_roundtrips:
        rt_by_day.setdefault(rt["session_date"], []).append(float(rt["pnl_usd"]))

    raw_days: dict[date, tuple[float, float, float]] = {}
    try:
        ph = client.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1M", timeframe="1D")
        )
        timestamps = list(getattr(ph, "timestamp", None) or [])
        equities = list(getattr(ph, "equity", None) or [])
        pls = list(getattr(ph, "profit_loss", None) or [])
        pl_pcts = list(getattr(ph, "profit_loss_pct", None) or [])

        for idx, ts_epoch in enumerate(timestamps):
            dt_ny = datetime.fromtimestamp(int(ts_epoch), tz=timezone.utc).astimezone(NY_TZ).date()
            eq_v = float(equities[idx] or 0.0) if idx < len(equities) else current_equity
            pl_v = float(pls[idx] or 0.0) if idx < len(pls) else 0.0
            pct_raw = float(pl_pcts[idx] or 0.0) if idx < len(pl_pcts) else 0.0
            # Alpaca 1D profit_loss_pct is decimal (e.g. 0.012 = 1.2%)
            pct_v = pct_raw * 100.0 if abs(pct_raw) < 1.0 else pct_raw
            raw_days[dt_ny] = (pl_v, pct_v, eq_v)
    except Exception:
        pass

    # Ensure today's live numbers are always reflected cleanly
    raw_days[today_ny] = (today_pnl_usd, today_pnl_pct, current_equity)

    # Filter out pre-bot flat $0.00 history rows so fresh accounts aren't cluttered with 23 empty days
    active_dates = sorted(
        set(orders_by_day.keys())
        | set(rt_by_day.keys())
        | {d for d, (pl, _, _) in raw_days.items() if abs(pl) >= 0.01}
    )
    earliest_active = active_dates[0] if active_dates else today_ny

    rows: list[DailyPnLRow] = []
    for sess_d in sorted(raw_days.keys(), reverse=True):
        if sess_d < earliest_active:
            continue
        pl_v, pct_v, eq_v = raw_days[sess_d]
        day_rts = rt_by_day.get(sess_d, [])
        wins = sum(1 for x in day_rts if x > 0)
        losses = sum(1 for x in day_rts if x <= 0)
        rows.append(
            DailyPnLRow(
                session_date=sess_d,
                date_label=sess_d.strftime("%a, %b %d, %Y"),
                trades_closed=len(day_rts),
                wins=wins,
                losses=losses,
                day_pnl_usd=round(pl_v, 2),
                day_return_pct=round(pct_v, 2),
                ending_equity_usd=round(eq_v, 2),
            )
        )

    if not rows:
        rows.append(
            DailyPnLRow(
                session_date=today_ny,
                date_label=today_ny.strftime("%a, %b %d, %Y"),
                trades_closed=0,
                wins=0,
                losses=0,
                day_pnl_usd=round(today_pnl_usd, 2),
                day_return_pct=round(today_pnl_pct, 2),
                ending_equity_usd=round(current_equity, 2),
            )
        )
    return rows

