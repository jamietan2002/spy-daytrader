"""Unit & headless UI tests enforcing Minimalist Matte-Dark UI rules, SGT timestamps, & P&L math."""

from __future__ import annotations

import ast
import asyncio
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from spy_daytrader.config import BotConfig
from spy_daytrader.dashboard_data import (
    DailyPnLRow,
    DashboardSnapshot,
    OpenTradeView,
    build_daily_takeaway,
    build_header_status,
    fetch_dashboard_snapshot,
    format_sgt,
    translate_order_action,
    translate_order_status,
    translate_skip_reason,
)
from spy_daytrader.live_stream import BrokerTradeStreamHub
from spy_daytrader.state_store import BotHeartbeat, SQLiteStateLedger

REPO_ROOT = Path(__file__).resolve().parents[1]
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]"
)
BANNED_JARGON = ("STOCHRSI", "EMA200", "EMA50", "R-MULTIPLE", "OCO", "PEAD", "SQN")


def test_zero_emojis_and_python311_syntax_across_ui_and_data_modules() -> None:
    targets = [
        REPO_ROOT / "dashboard" / "streamlit_app.py",
        REPO_ROOT / "src" / "spy_daytrader" / "dashboard_data.py",
        REPO_ROOT / "src" / "spy_daytrader" / "live_stream.py",
        REPO_ROOT / "src" / "spy_daytrader" / "supervisor.py",
    ]
    for path in targets:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
        match = EMOJI_RE.search(source)
        assert match is None, f"Prohibited emoji {match.group(0)!r} found in {path.name}"


def test_plain_english_translations_contain_zero_quant_jargon_and_sgt_format() -> None:
    utc_dt = datetime(2026, 11, 16, 14, 35, tzinfo=timezone.utc)  # 22:35 SGT
    sgt_lbl = format_sgt(utc_dt)
    assert sgt_lbl == "Nov 16, 22:35 SGT"

    # Remote GitHub Actions + Streamlit Cloud check: stale heartbeat (>10m) must not show yesterday's time
    old_hb = BotHeartbeat(
        symbol="SPY",
        mode="alpha_max",
        market_open=False,
        status="MARKET_CLOSED",
        next_open_utc=utc_dt,
        updated_at_utc=utc_dt - timedelta(hours=18),
    )
    open_remote_hdr = build_header_status(
        market_open=True,
        next_open_utc=utc_dt + timedelta(days=1),
        heartbeat=old_hb,
        reference_utc=utc_dt,
    )
    assert "Connected to Alpaca Paper" in open_remote_hdr
    assert "Last checked" not in open_remote_hdr

    sample_codes = [
        "OUTSIDE_ENTRY_WINDOW",
        "LONG_VETO_LUNCH_CHOP",
        "SHORT_VETO_MORNING_SQUEEZE",
        "LONG_VETO_BELOW_VWAP",
        "LONG_VETO_EMA50_BELOW_EMA200",
        "LONG_VETO_NON_BULLISH_CANDLE",
        "DAILY_CIRCUIT_BREAKER_HIT",
        "NO_SETUP",
    ]
    for code in sample_codes:
        plain = translate_skip_reason(code)
        assert EMOJI_RE.search(plain) is None
        for jargon in BANNED_JARGON:
            assert jargon not in plain.upper(), f"Leaked jargon {jargon} in {plain!r}"

    assert translate_order_action(side="BUY", is_entry=True, order_type="LIMIT") == "Entry (Buy)"
    assert translate_order_action(side="SELL", is_entry=False, order_type="STOP") == "Auto Stop-Loss (Sell)"
    assert translate_order_status("OrderStatus.FILLED") == "Filled"
    assert translate_order_status("canceled", "spydt-spy-amx-lng-1") == "Cancelled (Unfilled)"


def test_dashboard_snapshot_reconciles_open_trade_and_closed_roundtrip_pnl(tmp_path: Path) -> None:
    ledger = SQLiteStateLedger(tmp_path / "ui_state.db")
    now_utc = datetime(2026, 11, 16, 16, 10, tzinfo=timezone.utc)
    ledger.record_heartbeat(
        symbol="SPY",
        mode="alpha_max",
        market_open=True,
        status="HOLDING_OR_WORKING:OPEN_POSITION",
        next_open_utc=now_utc + timedelta(hours=18),
        updated_at_utc=now_utc,
    )
    ledger.mark_bar_processed("SPY", "alpha_max", now_utc, "NONE", "LONG_VETO_LUNCH_CHOP")

    # Mock one completed winning morning bracket (+$300) AND one currently open afternoon Long position
    t_morning_entry = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
    t_morning_exit = datetime(2026, 11, 16, 15, 25, tzinfo=timezone.utc)
    closed_tp_leg = SimpleNamespace(
        id="leg-tp-1",
        symbol="SPY",
        side="sell",
        type="limit",
        status="filled",
        qty="100",
        filled_qty="100",
        filled_avg_price="583.00",
        limit_price="583.00",
        stop_price=None,
        filled_at=t_morning_exit,
        submitted_at=t_morning_entry,
    )
    closed_sl_leg = SimpleNamespace(
        id="leg-sl-1",
        symbol="SPY",
        side="sell",
        type="stop",
        status="canceled",
        qty="100",
        filled_qty="0",
        filled_avg_price=None,
        limit_price=None,
        stop_price="578.00",
        filled_at=None,
        submitted_at=t_morning_entry,
    )
    closed_parent = SimpleNamespace(
        id="ord-parent-1",
        client_order_id="spydt-spy-amx-lng-20261116T1000",
        symbol="SPY",
        side="buy",
        type="limit",
        status="filled",
        qty="100",
        filled_qty="100",
        filled_avg_price="580.00",
        limit_price="580.08",
        stop_price=None,
        filled_at=t_morning_entry,
        submitted_at=t_morning_entry,
        legs=[closed_tp_leg, closed_sl_leg],
    )

    open_sl = SimpleNamespace(
        id="open-sl",
        symbol="SPY",
        side="sell",
        type="stop",
        status="new",
        stop_price="582.00",
        limit_price=None,
        legs=[],
    )
    open_tp = SimpleNamespace(
        id="open-tp",
        symbol="SPY",
        side="sell",
        type="limit",
        status="held",
        stop_price=None,
        limit_price="586.50",
        legs=[],
    )
    open_pos = SimpleNamespace(
        symbol="SPY",
        qty="200",
        avg_entry_price="584.00",
        current_price="585.10",
        market_value="117020.00",
        unrealized_pl="220.00",
        unrealized_plpc="0.00188",
    )

    class StubUIDataClient:
        def get_account(self) -> object:
            return SimpleNamespace(
                equity="100520.00",
                last_equity="100000.00",
                cash="42000.00",
                buying_power="282000.00",
            )

        def get_clock(self) -> object:
            return SimpleNamespace(
                is_open=True,
                timestamp=now_utc,
                next_open=now_utc + timedelta(hours=18),
            )

        def get_all_positions(self) -> list[object]:
            return [open_pos]

        def get_orders(self, filter: object = None) -> list[object]:  # noqa: A002
            st_val = str(getattr(filter, "status", "")).lower()
            if "open" in st_val:
                return [open_sl, open_tp]
            return [closed_parent]

        def get_portfolio_history(self, _req: object) -> object:
            return SimpleNamespace(
                timestamp=[int(now_utc.timestamp())],
                equity=[100520.0],
                profit_loss=[520.0],
                profit_loss_pct=[0.0052],
            )

    cfg = BotConfig(alpaca_api_key="k", alpaca_secret_key="s", state_db_path=str(tmp_path / "ui_state.db"))
    snap = fetch_dashboard_snapshot(cfg, trading_client=StubUIDataClient(), ledger=ledger)  # type: ignore[arg-type]

    assert snap.equity_usd == 100520.00
    assert snap.today_pnl_usd == 520.00
    assert snap.closed_trades_30d == 1
    assert snap.win_rate_30d_pct == 100.0
    assert snap.net_pnl_30d_usd == 300.00

    assert snap.open_trade is not None
    assert snap.open_trade.shares == 200
    assert snap.open_trade.stop_loss_price == 582.00
    assert snap.open_trade.max_loss_usd == 400.00
    assert snap.open_trade.profit_target_price == 586.50
    assert snap.open_trade.target_gain_usd == 500.00

    day_key = snap.available_days[0]
    takeaway = build_daily_takeaway(
        session_date=day_key,
        orders=snap.orders_by_day[day_key],
        day_pnl_usd=snap.today_pnl_usd,
        open_trade=snap.open_trade,
        is_latest_day=True,
    )
    assert "+$520.00" in takeaway

    # Verify GitHub-Secrets-Only mode: when Streamlit Cloud has NO local keys, it serves persisted dashboard_cache seamlessly!
    offline_cfg = BotConfig(
        alpaca_api_key="",
        alpaca_secret_key="",
        state_db_path=str(tmp_path / "ui_state.db"),
    )
    offline_snap = fetch_dashboard_snapshot(offline_cfg, ledger=ledger)
    assert offline_snap.equity_usd == 100520.00
    assert offline_snap.today_pnl_usd == 520.00
    assert offline_snap.open_trade is not None
    assert offline_snap.open_trade.shares == 200
    assert offline_snap.net_pnl_30d_usd == 300.00


def test_streamlit_app_renders_headlessly_without_exceptions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "")
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "headless.db"))

    at = AppTest.from_file(str(REPO_ROOT / "dashboard" / "streamlit_app.py"))
    at.run(timeout=10)
    assert not at.exception
    rendered_html = " ".join(m.value for m in at.markdown)
    assert "Missing ALPACA_API_KEY" not in rendered_html
    assert "Connection Configuration Needed" not in rendered_html
    assert "$100,000.00" in rendered_html


def test_broker_trade_stream_hub_event_driven_fills_and_zero_polling_when_flat(tmp_path: Path) -> None:
    now_utc = datetime(2026, 11, 16, 15, 10, tzinfo=timezone.utc)  # Mon 10:10 ET (market open)
    clock_mono = [1000.0]
    fetch_count = [0]
    hold_open_trade = [False]

    def _fake_fetch(cfg: BotConfig) -> DashboardSnapshot:
        fetch_count[0] += 1
        d_ny = date(2026, 11, 16)
        ot = (
            OpenTradeView(
                symbol=cfg.symbol,
                direction_label="Long (Bought)",
                shares=100,
                entry_price=580.0,
                current_price=581.5,
                market_value_usd=58150.0,
                unrealized_pnl_usd=150.0,
                unrealized_pnl_pct=0.26,
                stop_loss_price=578.0,
                max_loss_usd=200.0,
                profit_target_price=583.0,
                target_gain_usd=300.0,
                mandatory_close_label="04:55 SGT",
                plain_summary="Bought 100 shares of SPY.",
            )
            if hold_open_trade[0]
            else None
        )
        return DashboardSnapshot(
            symbol=cfg.symbol,
            equity_usd=100_000.0 + (150.0 if ot else 0.0),
            cash_usd=100_000.0,
            buying_power_usd=400_000.0,
            today_pnl_usd=150.0 if ot else 0.0,
            today_pnl_pct=0.15 if ot else 0.0,
            market_open=True,
            header_status_line="US Market Open · Autonomous bot active · Connected to Alpaca Paper",
            closed_trades_30d=0,
            win_rate_30d_pct=None,
            net_pnl_30d_usd=0.0,
            open_trade=ot,
            working_entry=None,
            orders_by_day={},
            skipped_by_day={},
            daily_pnl_rows=[
                DailyPnLRow(
                    session_date=d_ny,
                    date_label="Mon, Nov 16, 2026",
                    trades_closed=0,
                    wins=0,
                    losses=0,
                    day_pnl_usd=0.0,
                    day_return_pct=0.0,
                    ending_equity_usd=100_000.0,
                )
            ],
            available_days=[d_ny],
            next_open_utc=now_utc + timedelta(days=1),
        )

    subscribed_handlers: list[object] = []

    class FakeTradingStream:
        def __init__(self) -> None:
            self._running = False

        def subscribe_trade_updates(self, handler: object) -> None:
            subscribed_handlers.append(handler)

        def run(self) -> None:
            self._running = True
            while self._running:
                time.sleep(0.02)

        def stop(self) -> None:
            self._running = False

    cfg = BotConfig(
        alpaca_api_key="paper-key",
        alpaca_secret_key="paper-secret",
        state_db_path=str(tmp_path / "stream.db"),
    )
    hub = BrokerTradeStreamHub(
        cfg,
        fetch_fn=_fake_fetch,
        stream_factory=lambda _c: FakeTradingStream(),
        enable_websocket=True,
        coalesce_delay_sec=0.0,
        open_position_refresh_sec=60.0,
        monotonic_fn=lambda: clock_mono[0],
        now_fn=lambda: now_utc,
    ).start()

    try:
        # Wait briefly for FakeTradingStream daemon thread to enter run()
        deadline = time.time() + 2.0
        while not hub.stream_connected and time.time() < deadline:
            time.sleep(0.01)

        assert hub.stream_connected is True
        assert len(subscribed_handlers) == 1
        assert fetch_count[0] == 1

        # 1. Repeated UI interactions while flat must do ZERO extra REST fetches
        clock_mono[0] += 120.0
        snap1, v1, live1 = hub.get_snapshot(cfg)
        snap2, _v2, _live2 = hub.get_snapshot(cfg)
        assert live1 is True
        assert "Live fill stream active" in snap1.header_status_line
        assert snap2.equity_usd == 100_000.0
        assert fetch_count[0] == 1

        # 2. Irrelevant event does not hit REST API
        asyncio.run(
            hub.handle_trade_update(
                SimpleNamespace(event="pending_cancel", order=SimpleNamespace(symbol="SPY"))
            )
        )
        assert fetch_count[0] == 1

        # 3. Live Alpaca `fill` event wakes hub, refreshes RAM snapshot, and increments `version`
        hold_open_trade[0] = True
        asyncio.run(
            hub.handle_trade_update(
                SimpleNamespace(event="OrderEvent.FILL", order=SimpleNamespace(symbol="SPY"))
            )
        )
        snap3, v3, _ = hub.get_snapshot(cfg)
        assert fetch_count[0] == 2
        assert v3 > v1
        assert hub.last_event_name == "fill"
        assert snap3.open_trade is not None
        assert snap3.equity_usd == 100_150.0

        # 4. While holding an open trade, advancing >=60s triggers one non-blocking mark-to-market refresh
        clock_mono[0] += 65.0
        hub.check_background_refresh(cfg)
        deadline = time.time() + 2.0
        while fetch_count[0] < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert fetch_count[0] == 3
    finally:
        hub.stop()


