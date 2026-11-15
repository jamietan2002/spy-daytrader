"""Integration tests for SQLite crash recovery, stale limit cancel, external position protection, & CLI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from spy_daytrader.backtest import generate_synthetic_spy_5m_bars, run_backtest
from spy_daytrader.broker import AlpacaPaperBroker
from spy_daytrader.cli import main as cli_main
from spy_daytrader.config import BotConfig, StrategyMode
from spy_daytrader.data_feed import MarketDataFeed
from spy_daytrader.engine import IntradayTradingBot
from spy_daytrader.state_store import SQLiteStateLedger

NY_TZ = ZoneInfo("America/New_York")


class MockTradingClient:
    """In-memory mock of alpaca.trading.client.TradingClient."""

    def __init__(
        self,
        *,
        is_open: bool = True,
        positions: list[object] | None = None,
        open_orders: list[object] | None = None,
        raise_duplicate_cid: bool = False,
    ) -> None:
        self.is_open = is_open
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.submitted_orders: list[object] = []
        self.cancelled_ids: list[str] = []
        self.closed_symbols: list[str] = []
        self.raise_duplicate_cid = raise_duplicate_cid

    def get_account(self) -> object:
        return SimpleNamespace(
            equity="100000.00",
            last_equity="100000.00",
            buying_power="400000.00",
            cash="100000.00",
            trading_blocked=False,
            account_blocked=False,
            shorting_enabled=True,
        )

    def get_clock(self) -> object:
        now = datetime(2026, 10, 15, 14, 36, tzinfo=timezone.utc)
        return SimpleNamespace(
            is_open=self.is_open,
            timestamp=now,
            next_open=now + timedelta(hours=19),
            next_close=now + timedelta(hours=5),
        )

    def get_all_positions(self) -> list[object]:
        return list(self.positions)

    def get_orders(self, filter: object = None) -> list[object]:  # noqa: A002
        return list(self.open_orders)

    def submit_order(self, order_data: object) -> object:
        if self.raise_duplicate_cid:
            raise APIError('{"message":"client_order_id must be unique"}')
        self.submitted_orders.append(order_data)
        return SimpleNamespace(id="ord-12345", status="accepted")

    def cancel_order_by_id(self, order_id: object) -> None:
        self.cancelled_ids.append(str(order_id))
        self.open_orders = [o for p_o in [self.open_orders] for o in p_o if str(getattr(o, "id")) != str(order_id)]

    def close_position(self, symbol: str) -> None:
        self.closed_symbols.append(symbol)
        self.positions = [p for p in self.positions if getattr(p, "symbol") != symbol]


class StubDataFeed:
    def __init__(self, bars_df: pd.DataFrame, quote_px: float = 582.00) -> None:
        self._bars = bars_df
        self._quote_px = quote_px

    def fetch_closed_bars(self, **_kwargs: object) -> pd.DataFrame:
        return self._bars

    def fetch_executable_quote_price(self, _side: str, *, fallback_close: float) -> float:
        return self._quote_px or fallback_close


def _make_uptrend_oversold_cross_bars() -> pd.DataFrame:
    """Create 280 real OHLCV bars in a strong uptrend ending in a green-candle StochRSI <20 cross up."""
    end_ts = datetime(2026, 10, 15, 10, 30, tzinfo=NY_TZ)
    idx = pd.date_range(end=end_ts, periods=280, freq="5min", tz=NY_TZ)
    closes = np.linspace(555.0, 585.0, 280)
    for j in range(18):
        closes[-20 + j] = 585.0 - j * 0.25
    closes[-2] = 580.40
    closes[-1] = 582.00

    opens = closes - 0.20
    opens[-1] = 581.10  # Strong bullish reversal candle (close 582.00 > open 581.10)

    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) + 0.40,
            "low": np.minimum(opens, closes) - 0.40,
            "close": closes,
            "volume": 350_000,
        },
        index=idx,
    )


def test_sqlite_crash_recovery_prevents_double_order_after_bot_restart(tmp_path: Path) -> None:
    db_file = tmp_path / "bot_state.db"
    raw_df = _make_uptrend_oversold_cross_bars()
    cfg = BotConfig(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        strategy_mode=StrategyMode.ALPHA_MAX,
        state_db_path=str(db_file),
    )
    mock_client = MockTradingClient()
    now_t1 = datetime(2026, 10, 15, 10, 35, 2, tzinfo=NY_TZ).astimezone(timezone.utc)

    # Instance #1 submits Marketable Limit Bracket
    ledger1 = SQLiteStateLedger(db_file)
    broker1 = AlpacaPaperBroker(cfg, trading_client=mock_client, ledger=ledger1)  # type: ignore[arg-type]
    bot1 = IntradayTradingBot(
        cfg, data_feed=StubDataFeed(raw_df, 582.00), broker=broker1, ledger=ledger1, now_fn=lambda: now_t1  # type: ignore[arg-type]
    )
    out1 = bot1.run_once()
    assert out1.status == "ORDER_SUBMITTED"
    assert len(mock_client.submitted_orders) == 1
    assert isinstance(mock_client.submitted_orders[0], LimitOrderRequest)
    assert mock_client.submitted_orders[0].order_class == OrderClass.BRACKET
    assert mock_client.submitted_orders[0].time_in_force == TimeInForce.DAY

    # Simulate process crash & brand-new IntradayTradingBot process #2 20 seconds later on same 5m bar!
    ledger2 = SQLiteStateLedger(db_file)
    broker2 = AlpacaPaperBroker(cfg, trading_client=mock_client, ledger=ledger2)  # type: ignore[arg-type]
    bot2 = IntradayTradingBot(
        cfg, data_feed=StubDataFeed(raw_df, 582.00), broker=broker2, ledger=ledger2, now_fn=lambda: now_t1  # type: ignore[arg-type]
    )
    out2 = bot2.run_once()
    assert out2.status == "BAR_ALREADY_PROCESSED"
    assert len(mock_client.submitted_orders) == 1


def test_baseline_submits_market_bracket_and_stale_limit_auto_cancels(tmp_path: Path) -> None:
    raw_df = _make_uptrend_oversold_cross_bars()
    cfg_base = BotConfig(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        strategy_mode=StrategyMode.BASELINE,
        state_db_path=str(tmp_path / "base.db"),
    )
    now_t1 = datetime(2026, 10, 15, 10, 35, 2, tzinfo=NY_TZ).astimezone(timezone.utc)

    # Stale unfilled spydt-* order from 75 seconds ago -> must be auto-cancelled before evaluation!
    stale_order = SimpleNamespace(
        id="stale-lim-99",
        client_order_id="spydt-spy-amx-lng-20261015T1030",
        symbol="SPY",
        status="new",
        filled_qty="0",
        submitted_at=now_t1 - timedelta(seconds=75),
    )
    mock_client = MockTradingClient(open_orders=[stale_order])
    ledger = SQLiteStateLedger(cfg_base.state_db_path)
    broker = AlpacaPaperBroker(cfg_base, trading_client=mock_client, ledger=ledger)  # type: ignore[arg-type]
    bot = IntradayTradingBot(
        cfg_base, data_feed=StubDataFeed(raw_df, 582.00), broker=broker, ledger=ledger, now_fn=lambda: now_t1  # type: ignore[arg-type]
    )

    out = bot.run_once()
    assert "stale-lim-99" in mock_client.cancelled_ids
    assert out.status == "ORDER_SUBMITTED"
    assert isinstance(mock_client.submitted_orders[0], MarketOrderRequest)
    assert mock_client.submitted_orders[0].side == OrderSide.BUY

    bot.run_forever(dry_run=True, max_cycles=1)


def test_eod_flatten_protects_external_swing_positions_and_flattens_bot_owned(tmp_path: Path) -> None:
    cfg = BotConfig(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        state_db_path=str(tmp_path / "eod.db"),
    )
    ledger = SQLiteStateLedger(cfg.state_db_path)
    now_1556 = datetime(2026, 10, 15, 15, 56, 0, tzinfo=NY_TZ).astimezone(timezone.utc)

    # Case A: SPY position held by external swing bot (no spydt-* order in ledger or open orders) -> PROTECTED!
    ext_client = MockTradingClient(
        positions=[SimpleNamespace(symbol="SPY", qty="50")],
        open_orders=[SimpleNamespace(id="swing-stop", client_order_id="swing-spy-1", symbol="SPY", status="new")],
    )
    ext_broker = AlpacaPaperBroker(cfg, trading_client=ext_client, ledger=ledger)  # type: ignore[arg-type]
    ext_bot = IntradayTradingBot(
        cfg, data_feed=StubDataFeed(generate_synthetic_spy_5m_bars(5)), broker=ext_broker, ledger=ledger, now_fn=lambda: now_1556  # type: ignore[arg-type]
    )
    assert ext_bot.run_once().status == "EOD_EXTERNAL_POSITION_PROTECTED"
    assert ext_client.closed_symbols == []
    assert ext_client.cancelled_ids == []

    # Case B: Bot recorded a spydt-* order today -> cancels child legs first & flattens position!
    ledger.record_order_submission(
        client_order_id="spydt-spy-amx-lng-20261015T1335",
        broker_order_id="ord-1",
        symbol="SPY",
        mode="alpha_max",
        side="BUY",
        order_type="LIMIT",
        qty=150,
        reference_price=580.0,
        limit_price=580.08,
        stop_loss_price=578.80,
        take_profit_price=581.80,
        status="filled",
        session_date=datetime(2026, 10, 15, tzinfo=NY_TZ).date(),
    )
    own_client = MockTradingClient(
        positions=[SimpleNamespace(symbol="SPY", qty="150")],
        open_orders=[
            SimpleNamespace(id="tp-leg", client_order_id="", symbol="SPY", status="held"),
            SimpleNamespace(id="sl-leg", client_order_id="", symbol="SPY", status="new"),
        ],
    )
    own_broker = AlpacaPaperBroker(cfg, trading_client=own_client, ledger=ledger)  # type: ignore[arg-type]
    own_bot = IntradayTradingBot(
        cfg, data_feed=StubDataFeed(generate_synthetic_spy_5m_bars(5)), broker=own_broker, ledger=ledger, now_fn=lambda: now_1556  # type: ignore[arg-type]
    )
    assert own_bot.run_once().status == "EOD_FLATTENED"
    assert own_client.cancelled_ids == ["tp-leg", "sl-leg"]
    assert own_client.closed_symbols == ["SPY"]


def test_quote_anchor_duplicate_cid_and_cli_compare(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = BotConfig(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        strategy_mode=StrategyMode.BASELINE,
        state_db_path=str(tmp_path / "dup.db"),
    )

    class QuoteStubClient:
        def get_stock_latest_quote(self, _req: object) -> dict[str, object]:
            return {"SPY": SimpleNamespace(ask_price=582.12, bid_price=582.10)}

    feed = MarketDataFeed(cfg, client=QuoteStubClient())  # type: ignore[arg-type]
    assert feed.fetch_executable_quote_price("BUY", fallback_close=582.00) == 582.12
    assert feed.fetch_executable_quote_price("SELL", fallback_close=582.00) == 582.10

    raw_df = _make_uptrend_oversold_cross_bars()
    now_t1 = datetime(2026, 10, 15, 10, 35, 5, tzinfo=NY_TZ).astimezone(timezone.utc)
    dup_broker = AlpacaPaperBroker(
        cfg, trading_client=MockTradingClient(raise_duplicate_cid=True)  # type: ignore[arg-type]
    )
    bot_dup = IntradayTradingBot(
        cfg, data_feed=StubDataFeed(raw_df), broker=dup_broker, now_fn=lambda: now_t1  # type: ignore[arg-type]
    )
    assert bot_dup.run_once().status == "ORDER_NOT_SUBMITTED:DUPLICATE_SKIPPED"

    bars = generate_synthetic_spy_5m_bars(sessions=24, seed=7)
    rep_base = run_backtest(bars, BotConfig(strategy_mode=StrategyMode.BASELINE))
    rep_alpha = run_backtest(bars, BotConfig(strategy_mode=StrategyMode.ALPHA_MAX))
    for trade in rep_base.trades + rep_alpha.trades:
        assert trade.entry_bar_open_et.date() == trade.exit_bar_et.date()

    rc = cli_main(["backtest", "--synthetic", "--days", "24", "--compare"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "baseline/std" in out
    assert "alpha_max/std" in out
    assert "alpha_max/wide" in out


def test_run_market_session_holiday_exit_cutoff_and_halfday_flatten(tmp_path: Path) -> None:
    from datetime import time as dt_time

    cfg = BotConfig(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        poll_interval_seconds=10,
        state_db_path=str(tmp_path / "sess.db"),
    )
    ledger = SQLiteStateLedger(cfg.state_db_path)

    # 1. Holiday / weekend instant exit (market closed today; next_open is tomorrow)
    now_holiday = datetime(2026, 11, 26, 10, 0, 0, tzinfo=NY_TZ).astimezone(timezone.utc)
    next_open_fri = datetime(2026, 11, 27, 9, 30, 0, tzinfo=NY_TZ).astimezone(timezone.utc)
    hol_client = MockTradingClient(is_open=False)
    hol_client.get_clock = lambda: SimpleNamespace(  # type: ignore[method-assign]
        is_open=False,
        timestamp=now_holiday,
        next_open=next_open_fri,
        next_close=next_open_fri,
    )
    hol_broker = AlpacaPaperBroker(cfg, trading_client=hol_client, ledger=ledger)  # type: ignore[arg-type]
    hol_bot = IntradayTradingBot(
        cfg,
        data_feed=StubDataFeed(generate_synthetic_spy_5m_bars(5)),
        broker=hol_broker,
        ledger=ledger,
        now_fn=lambda: now_holiday,  # type: ignore[arg-type]
    )
    assert hol_bot.run_market_session(register_signals=False) == 0
    hb = ledger.get_latest_heartbeat()
    assert hb is not None and hb.status == "MARKET_CLOSED"

    # 2. Early-close half-day (Black Friday 12:56 ET, 4m before 13:00 ET close) triggers EOD flatten!
    now_halfday = datetime(2026, 11, 27, 12, 56, 0, tzinfo=NY_TZ).astimezone(timezone.utc)
    close_halfday = datetime(2026, 11, 27, 13, 0, 0, tzinfo=NY_TZ).astimezone(timezone.utc)
    ledger.record_order_submission(
        client_order_id="spydt-spy-amx-lng-20261127T1015",
        broker_order_id="ord-bf",
        symbol="SPY",
        mode="alpha_max",
        side="BUY",
        order_type="LIMIT",
        qty=50,
        reference_price=585.0,
        limit_price=585.05,
        stop_loss_price=583.5,
        take_profit_price=587.25,
        status="filled",
        session_date=datetime(2026, 11, 27, tzinfo=NY_TZ).date(),
    )
    bf_client = MockTradingClient(positions=[SimpleNamespace(symbol="SPY", qty="50")])
    bf_client.get_clock = lambda: SimpleNamespace(  # type: ignore[method-assign]
        is_open=True,
        timestamp=now_halfday,
        next_open=datetime(2026, 11, 30, 9, 30, 0, tzinfo=NY_TZ).astimezone(timezone.utc),
        next_close=close_halfday,
    )
    bf_broker = AlpacaPaperBroker(cfg, trading_client=bf_client, ledger=ledger)  # type: ignore[arg-type]
    bf_bot = IntradayTradingBot(
        cfg,
        data_feed=StubDataFeed(generate_synthetic_spy_5m_bars(5)),
        broker=bf_broker,
        ledger=ledger,
        now_fn=lambda: now_halfday,  # type: ignore[arg-type]
    )
    assert bf_bot.run_market_session(until_et=dt_time(12, 55), register_signals=False) == 0
    assert bf_client.closed_symbols == []  # exited cleanly because 12:56 >= 12:55 cutoff

    assert bf_bot.run_market_session(until_et=dt_time(13, 5), max_cycles=1, register_signals=False) == 0
    assert bf_client.closed_symbols == ["SPY"]
