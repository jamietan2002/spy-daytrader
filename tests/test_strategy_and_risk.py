"""Unit tests for v2 signal crossover logic, asymmetric session gates, ATR profiles, and limit collars."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from spy_daytrader.config import ATRProfile, BotConfig, StrategyMode
from spy_daytrader.risk import AccountSnapshot, calculate_bracket_order_plan
from spy_daytrader.state_store import BOT_ORDER_PREFIX
from spy_daytrader.strategy import SignalAction, evaluate_signal

NY_TZ = ZoneInfo("America/New_York")


def _make_enriched_window(
    *,
    open_px: float | None = None,
    close: float = 582.0,
    ema_200: float = 580.0,
    ema_50: float = 581.2,
    ema_slope: float = 0.25,
    vwap: float = 581.0,
    k_prev: float = 15.0,
    d_prev: float = 17.0,
    k_curr: float = 22.0,
    d_curr: float = 18.0,
    atr_14: float = 0.80,
    last_bar_open_hm: tuple[int, int] = (10, 30),
    rows: int = 210,
) -> pd.DataFrame:
    end_ts = datetime(2026, 10, 15, last_bar_open_hm[0], last_bar_open_hm[1], tzinfo=NY_TZ)
    idx = pd.date_range(end=end_ts, periods=rows, freq="5min", tz=NY_TZ)
    eff_open = (close - 0.25) if open_px is None else open_px
    df = pd.DataFrame(
        {
            "open": eff_open,
            "high": max(eff_open, close) + 0.3,
            "low": min(eff_open, close) - 0.3,
            "close": close,
            "volume": 300_000,
            "ema_200": ema_200,
            "ema_50": ema_50,
            "ema_200_slope": ema_slope,
            "session_vwap": vwap,
            "stochrsi_k": 50.0,
            "stochrsi_d": 50.0,
            "atr_14": atr_14,
            "atr_pct": atr_14 / close,
            "is_bull_candle": close > eff_open,
            "is_bear_candle": close < eff_open,
        },
        index=idx,
    )
    df.iloc[-2, df.columns.get_loc("stochrsi_k")] = k_prev
    df.iloc[-2, df.columns.get_loc("stochrsi_d")] = d_prev
    df.iloc[-1, df.columns.get_loc("stochrsi_k")] = k_curr
    df.iloc[-1, df.columns.get_loc("stochrsi_d")] = d_curr
    return df


def test_long_signal_fires_on_valid_stochrsi_cross_below_20() -> None:
    df = _make_enriched_window()
    cfg = BotConfig(strategy_mode=StrategyMode.ALPHA_MAX)
    sig = evaluate_signal(df, cfg)
    assert sig.action == SignalAction.BUY
    assert "LONG_SIGNAL" in sig.reason


def test_alpha_max_vetoes_lunch_chop_and_non_bull_candle_while_baseline_allows() -> None:
    # 11:30 ET bar open -> 11:35 ET close (inside 11:00-13:00 NYC lunch chop)
    df_lunch = _make_enriched_window(last_bar_open_hm=(11, 30))
    sig_lunch = evaluate_signal(df_lunch, BotConfig(strategy_mode=StrategyMode.ALPHA_MAX))
    assert sig_lunch.action == SignalAction.NONE
    assert "LONG_VETO_LUNCH_CHOP" in sig_lunch.reason
    assert evaluate_signal(df_lunch, BotConfig(strategy_mode=StrategyMode.BASELINE)).action == SignalAction.BUY

    # Red candle on crossover bar (open > close) vetoed in alpha_max
    df_red = _make_enriched_window(open_px=582.5, close=582.0, last_bar_open_hm=(10, 30))
    sig_red = evaluate_signal(df_red, BotConfig(strategy_mode=StrategyMode.ALPHA_MAX))
    assert sig_red.action == SignalAction.NONE
    assert "LONG_VETO_NON_BULLISH_CANDLE" in sig_red.reason


def test_alpha_max_vetoes_long_below_vwap_and_post_1500_cutoff() -> None:
    df = _make_enriched_window(close=582.0, ema_200=580.0, vwap=584.0)
    sig_alpha = evaluate_signal(df, BotConfig(strategy_mode=StrategyMode.ALPHA_MAX))
    assert sig_alpha.action == SignalAction.NONE
    assert "LONG_VETO_BELOW_VWAP" in sig_alpha.reason

    # 15:00 ET bar open -> closes 15:05 ET (> 15:00 ET alpha_entry_cutoff_et)
    df_late = _make_enriched_window(last_bar_open_hm=(15, 0))
    sig_late = evaluate_signal(df_late, BotConfig(strategy_mode=StrategyMode.ALPHA_MAX))
    assert sig_late.action == SignalAction.NONE
    assert "OUTSIDE_ENTRY_WINDOW" in sig_late.reason


def test_symmetric_short_respects_morning_squeeze_gate_and_fires_afternoon() -> None:
    # 10:15 ET bar open (closes 10:20 ET < 11:00 ET) -> vetoed by morning squeeze guard
    df_am = _make_enriched_window(
        close=575.0,
        ema_200=578.0,
        ema_50=576.0,
        ema_slope=-0.30,
        vwap=576.5,
        k_prev=86.0,
        d_prev=84.0,
        k_curr=78.0,
        d_curr=82.0,
        last_bar_open_hm=(10, 15),
    )
    sig_am = evaluate_signal(df_am, BotConfig(strategy_mode=StrategyMode.ALPHA_MAX))
    assert sig_am.action == SignalAction.NONE
    assert "SHORT_VETO_MORNING_SQUEEZE" in sig_am.reason

    # 13:15 ET bar open (closes 13:20 ET >= 11:00 ET) -> fires SELL_SHORT in alpha_max, NONE in baseline
    df_pm = _make_enriched_window(
        close=575.0,
        ema_200=578.0,
        ema_50=576.0,
        ema_slope=-0.30,
        vwap=576.5,
        k_prev=86.0,
        d_prev=84.0,
        k_curr=78.0,
        d_curr=82.0,
        last_bar_open_hm=(13, 15),
    )
    assert evaluate_signal(df_pm, BotConfig(strategy_mode=StrategyMode.ALPHA_MAX)).action == SignalAction.SELL_SHORT
    assert evaluate_signal(df_pm, BotConfig(strategy_mode=StrategyMode.BASELINE)).action == SignalAction.NONE


def test_marketable_limit_collar_in_alpha_max_vs_market_in_baseline_and_wide_profile() -> None:
    acct = AccountSnapshot(
        equity=100_000.0,
        last_equity=100_000.0,
        buying_power=400_000.0,
        cash=100_000.0,
    )
    ts = datetime(2026, 10, 15, 10, 35, tzinfo=NY_TZ)

    # Baseline -> MARKET order, 1.5x/2.25x ATR
    plan_base = calculate_bracket_order_plan(
        account=acct,
        side="BUY",
        reference_price=580.00,
        atr_14=2.00,
        bar_close_et=ts,
        config=BotConfig(strategy_mode=StrategyMode.BASELINE),
    )
    assert plan_base.approved is True
    assert plan_base.order_type == "MARKET"
    assert plan_base.limit_price is None
    assert plan_base.client_order_id.startswith(BOT_ORDER_PREFIX)
    assert plan_base.qty == 333
    assert plan_base.stop_loss_price == 577.00
    assert plan_base.take_profit_price == 584.50

    # Alpha-Max + Wide Profile -> LIMIT order with collar (0.08 * 2.00 = $0.16), 2.0x/3.0x ATR
    plan_wide = calculate_bracket_order_plan(
        account=acct,
        side="BUY",
        reference_price=580.00,
        atr_14=2.00,
        bar_close_et=ts,
        config=BotConfig(strategy_mode=StrategyMode.ALPHA_MAX, atr_profile=ATRProfile.WIDE),
    )
    assert plan_wide.approved is True
    assert plan_wide.order_type == "LIMIT"
    assert plan_wide.limit_price == 580.16
    assert plan_wide.stop_loss_price == 576.00  # 580 - 2.0 * 2.0
    assert plan_wide.take_profit_price == 586.00  # 580 + 3.0 * 2.0
    assert plan_wide.reward_to_risk == pytest.approx(1.5, rel=1e-3)


def test_bot_scoped_and_account_circuit_breakers() -> None:
    # Account daily P&L is +1.0% (from external swing gains), but bot-scoped SQLite P&L is -$2,600 (-2.6%)!
    acct = AccountSnapshot(
        equity=100_000.0,
        last_equity=99_000.0,
        buying_power=380_000.0,
        cash=100_000.0,
    )
    plan = calculate_bracket_order_plan(
        account=acct,
        side="BUY",
        reference_price=580.0,
        atr_14=1.20,
        bar_close_et=datetime(2026, 10, 15, 13, 30, tzinfo=NY_TZ),
        config=BotConfig(),
        bot_session_realized_pnl_usd=-2_600.0,
    )
    assert plan.approved is False
    assert "BOT_DAILY_CIRCUIT_BREAKER_HIT" in plan.veto_reason
