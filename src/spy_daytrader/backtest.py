"""Chronological event-driven 5-minute intraday backtester with marketable limit collar physics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import numpy as np
import pandas as pd

from spy_daytrader.config import ATRProfile, BotConfig, StrategyMode
from spy_daytrader.indicators import compute_indicators
from spy_daytrader.risk import AccountSnapshot, calculate_bracket_order_plan
from spy_daytrader.strategy import SignalAction, evaluate_signal


@dataclass(frozen=True)
class SimulatedTrade:
    """Completed simulated intraday bracket round-trip."""

    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"]
    qty: int
    signal_bar_close_et: datetime
    entry_bar_open_et: datetime
    exit_bar_et: datetime
    entry_price: float
    exit_price: float
    stop_loss_price: float
    take_profit_price: float
    risk_usd: float
    pnl_usd: float
    r_multiple: float
    exit_reason: Literal["TAKE_PROFIT", "STOP_LOSS", "EOD_FLATTEN"]


@dataclass(frozen=True)
class BacktestReport:
    """Comprehensive quantitative performance summary for a 5-minute strategy run."""

    mode: StrategyMode
    atr_profile: ATRProfile
    symbol: str
    bars_tested: int
    sessions_tested: int
    initial_equity: float
    final_equity: float
    net_profit_usd: float
    total_return_pct: float
    total_trades: int
    long_trades: int
    short_trades: int
    missed_limit_entries: int
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: float
    expectancy_r: float
    avg_risk_pct_deployed: float
    avg_win_usd: float
    avg_loss_usd: float
    max_drawdown_pct: float
    sharpe_annualized: float
    trades: list[SimulatedTrade]


@dataclass
class _OpenSimPosition:
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"]
    qty: int
    signal_bar_close_et: datetime
    entry_bar_open_et: datetime
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    risk_usd: float
    equity_at_entry: float


def run_backtest(
    bars_df: pd.DataFrame,
    config: BotConfig,
    *,
    initial_equity: float = 100_000.0,
    buying_power_mult: float = 4.0,
    slippage_bps: float = 1.0,
) -> BacktestReport:
    """
    Execute an institutional bar-by-bar 5m backtest:
      - Causal indicator computation up to bar `i`
      - Signal at bar `i` close submits Market or Marketable Limit bracket at bar `i + 1` open:
          * If `LIMIT`: fills if bar `i + 1` open/range trades within `limit_price`; otherwise
            expires cancelled after 1m (`missed_limit_entries += 1`, no chasing!)
      - Conservative intra-bar tie-breaking: if both SL and TP hit in the same 5m bar, SL wins
      - Mandatory intraday EOD flatten at >= 15:55 ET (zero overnight gap exposure)
      - Daily -2.5% drawdown circuit breaker & post-stop cooldown enforced identically to live engine
    """
    enriched = compute_indicators(bars_df, config)
    min_warmup = config.ema_length + config.ema_slope_lookback + 2
    if enriched.empty or len(enriched) <= min_warmup + 1:
        return _empty_report(config, initial_equity, len(enriched))

    equity = float(initial_equity)
    day_start_equity = float(initial_equity)
    current_session_date: date | None = None
    daily_returns: list[float] = []
    peak_equity = equity
    max_dd = 0.0

    trades: list[SimulatedTrade] = []
    missed_limits = 0
    open_pos: _OpenSimPosition | None = None
    pending_signal_side: Literal["BUY", "SELL"] | None = None
    pending_signal_ref_px: float = 0.0
    pending_signal_atr: float = 0.0
    pending_signal_close_et: datetime | None = None
    cooldown_remaining = 0

    slip_factor = slippage_bps / 10_000.0
    timestamps = list(enriched.index)

    for i in range(min_warmup, len(enriched)):
        row = enriched.iloc[i]
        ts_open = pd.Timestamp(timestamps[i]).to_pydatetime()
        ts_close = (
            pd.Timestamp(timestamps[i]) + pd.Timedelta(minutes=config.bar_timeframe_minutes)
        ).to_pydatetime()
        row_date = ts_open.date()

        if current_session_date is None:
            current_session_date = row_date
            day_start_equity = equity
        elif row_date != current_session_date:
            if day_start_equity > 0:
                daily_returns.append((equity - day_start_equity) / day_start_equity)
            current_session_date = row_date
            day_start_equity = equity
            pending_signal_side = None
            cooldown_remaining = 0

        bar_open = float(row["open"])
        bar_high = float(row["high"])
        bar_low = float(row["low"])
        bar_close = float(row["close"])

        # 1. Execute pending signal from bar i-1 close at bar i open
        if pending_signal_side is not None and open_pos is None and pending_signal_close_et is not None:
            if ts_open.time() < config.eod_flatten_et:
                acct = AccountSnapshot(
                    equity=equity,
                    last_equity=day_start_equity,
                    buying_power=equity * buying_power_mult,
                    cash=equity,
                )
                # Build bracket anchored at live bar open (or quote at boundary)
                mkt_open_with_slip = (
                    round(bar_open * (1.0 + slip_factor), 2)
                    if pending_signal_side == "BUY"
                    else round(bar_open * (1.0 - slip_factor), 2)
                )
                anchor_px = pending_signal_ref_px if config.use_marketable_limit_entry else mkt_open_with_slip
                plan = calculate_bracket_order_plan(
                    account=acct,
                    side=pending_signal_side,
                    reference_price=anchor_px,
                    atr_14=pending_signal_atr,
                    bar_close_et=pending_signal_close_et,
                    config=config,
                    bot_session_realized_pnl_usd=equity - day_start_equity,
                )

                if plan.approved and plan.qty >= 1:
                    filled = True
                    actual_fill_px = mkt_open_with_slip
                    if plan.order_type == "LIMIT" and plan.limit_price is not None:
                        if plan.side == "BUY":
                            if mkt_open_with_slip <= plan.limit_price:
                                actual_fill_px = mkt_open_with_slip
                            elif bar_low <= plan.limit_price:
                                actual_fill_px = plan.limit_price
                            else:
                                filled = False
                                missed_limits += 1
                        else:
                            if mkt_open_with_slip >= plan.limit_price:
                                actual_fill_px = mkt_open_with_slip
                            elif bar_high >= plan.limit_price:
                                actual_fill_px = plan.limit_price
                            else:
                                filled = False
                                missed_limits += 1

                    if filled:
                        open_pos = _OpenSimPosition(
                            side=plan.side,
                            order_type=plan.order_type,
                            qty=plan.qty,
                            signal_bar_close_et=pending_signal_close_et,
                            entry_bar_open_et=ts_open,
                            entry_price=actual_fill_px,
                            stop_loss_price=plan.stop_loss_price,
                            take_profit_price=plan.take_profit_price,
                            risk_usd=plan.actual_risk_usd,
                            equity_at_entry=equity,
                        )
            pending_signal_side = None
            pending_signal_close_et = None

        # 2. Evaluate open bracket position across bar i [open, high, low, close]
        if open_pos is not None:
            exit_reason: Literal["TAKE_PROFIT", "STOP_LOSS", "EOD_FLATTEN"] | None = None
            exit_px = 0.0

            if open_pos.side == "BUY":
                hit_sl = bar_low <= open_pos.stop_loss_price
                hit_tp = bar_high >= open_pos.take_profit_price
                if hit_sl:
                    exit_reason = "STOP_LOSS"
                    exit_px = round(open_pos.stop_loss_price * (1.0 - slip_factor), 2)
                elif hit_tp:
                    exit_reason = "TAKE_PROFIT"
                    exit_px = open_pos.take_profit_price
                elif ts_close.time() >= config.eod_flatten_et:
                    exit_reason = "EOD_FLATTEN"
                    exit_px = round(bar_close * (1.0 - slip_factor), 2)
            else:
                hit_sl = bar_high >= open_pos.stop_loss_price
                hit_tp = bar_low <= open_pos.take_profit_price
                if hit_sl:
                    exit_reason = "STOP_LOSS"
                    exit_px = round(open_pos.stop_loss_price * (1.0 + slip_factor), 2)
                elif hit_tp:
                    exit_reason = "TAKE_PROFIT"
                    exit_px = open_pos.take_profit_price
                elif ts_close.time() >= config.eod_flatten_et:
                    exit_reason = "EOD_FLATTEN"
                    exit_px = round(bar_close * (1.0 + slip_factor), 2)

            if exit_reason is not None:
                pnl = (
                    (exit_px - open_pos.entry_price) * open_pos.qty
                    if open_pos.side == "BUY"
                    else (open_pos.entry_price - exit_px) * open_pos.qty
                )
                r_mult = pnl / open_pos.risk_usd if open_pos.risk_usd > 0 else 0.0
                equity += pnl
                peak_equity = max(peak_equity, equity)
                if peak_equity > 0:
                    max_dd = max(max_dd, (peak_equity - equity) / peak_equity)

                trades.append(
                    SimulatedTrade(
                        symbol=config.symbol,
                        side=open_pos.side,
                        order_type=open_pos.order_type,
                        qty=open_pos.qty,
                        signal_bar_close_et=open_pos.signal_bar_close_et,
                        entry_bar_open_et=open_pos.entry_bar_open_et,
                        exit_bar_et=ts_close,
                        entry_price=open_pos.entry_price,
                        exit_price=exit_px,
                        stop_loss_price=open_pos.stop_loss_price,
                        take_profit_price=open_pos.take_profit_price,
                        risk_usd=open_pos.risk_usd,
                        pnl_usd=round(pnl, 2),
                        r_multiple=round(r_mult, 3),
                        exit_reason=exit_reason,
                    )
                )
                if exit_reason == "STOP_LOSS" and config.strategy_mode == StrategyMode.ALPHA_MAX:
                    cooldown_remaining = config.cooldown_bars_after_stop
                open_pos = None
                continue

        # 3. If flat at bar i close, evaluate causal slice
        if open_pos is None:
            if cooldown_remaining > 0:
                cooldown_remaining -= 1
                continue
            if i + 1 >= len(enriched):
                continue
            next_open_ts = pd.Timestamp(timestamps[i + 1]).to_pydatetime()
            if next_open_ts.date() != row_date or next_open_ts.time() >= config.eod_flatten_et:
                continue

            window = enriched.iloc[max(0, i + 1 - (config.ema_length + 30)) : i + 1]
            decision = evaluate_signal(window, config)
            if decision.action == SignalAction.BUY:
                pending_signal_side = "BUY"
                pending_signal_ref_px = decision.close
                pending_signal_atr = decision.atr_14
                pending_signal_close_et = ts_close
            elif decision.action == SignalAction.SELL_SHORT:
                pending_signal_side = "SELL"
                pending_signal_ref_px = decision.close
                pending_signal_atr = decision.atr_14
                pending_signal_close_et = ts_close

    if current_session_date is not None and day_start_equity > 0:
        daily_returns.append((equity - day_start_equity) / day_start_equity)

    return _summarize_trades(
        config=config,
        bars_tested=len(enriched),
        sessions_tested=len({pd.Timestamp(ts).date() for ts in timestamps}),
        initial_equity=initial_equity,
        final_equity=equity,
        max_dd=max_dd,
        missed_limits=missed_limits,
        daily_returns=daily_returns,
        trades=trades,
    )


def generate_synthetic_spy_5m_bars(sessions: int = 25, seed: int = 42) -> pd.DataFrame:
    """Generate realistic intraday 5m SPY RTH bars with alternating bullish & bearish trend regimes."""
    rng = np.random.default_rng(seed)
    business_days = pd.bdate_range("2026-09-01", periods=sessions)
    records: list[dict[str, object]] = []
    price = 575.0

    for day_idx, bday in enumerate(business_days):
        regime_dir = 1.0 if (day_idx // 4) % 2 == 0 else -1.0
        price *= 1.0 + regime_dir * 0.0008
        for bar_i in range(78):
            ts = pd.Timestamp(
                year=bday.year,
                month=bday.month,
                day=bday.day,
                hour=9,
                minute=30,
                tz="America/New_York",
            ) + pd.Timedelta(minutes=5 * bar_i)

            wave = math.sin(bar_i * 2.0 * math.pi / 16.0) * 0.38
            drift = regime_dir * 0.075
            shock = float(rng.normal(0.0, 0.14))
            open_px = price
            close_px = max(50.0, open_px + drift + wave * 0.28 + shock)
            spread = abs(float(rng.normal(0.32, 0.08)))
            high_px = max(open_px, close_px) + spread * 0.55
            low_px = min(open_px, close_px) - spread * 0.55
            vol = int(rng.integers(250_000, 950_000))
            records.append(
                {
                    "timestamp": ts,
                    "open": round(open_px, 2),
                    "high": round(high_px, 2),
                    "low": round(low_px, 2),
                    "close": round(close_px, 2),
                    "volume": vol,
                }
            )
            price = close_px

    return pd.DataFrame.from_records(records).set_index("timestamp")


def _summarize_trades(
    *,
    config: BotConfig,
    bars_tested: int,
    sessions_tested: int,
    initial_equity: float,
    final_equity: float,
    max_dd: float,
    missed_limits: int,
    daily_returns: list[float],
    trades: list[SimulatedTrade],
) -> BacktestReport:
    total = len(trades)
    long_cnt = sum(1 for t in trades if t.side == "BUY")
    short_cnt = sum(1 for t in trades if t.side == "SELL")
    wins_list = [t for t in trades if t.pnl_usd > 0]
    losses_list = [t for t in trades if t.pnl_usd <= 0]

    gross_profit = sum(t.pnl_usd for t in wins_list)
    gross_loss = abs(sum(t.pnl_usd for t in losses_list))
    pf = (
        round(gross_profit / gross_loss, 2)
        if gross_loss > 0
        else (99.99 if gross_profit > 0 else 0.0)
    )

    win_rate = (len(wins_list) / total * 100.0) if total > 0 else 0.0
    expectancy_r = (sum(t.r_multiple for t in trades) / total) if total > 0 else 0.0
    avg_risk_pct = (
        sum(t.risk_usd / initial_equity * 100.0 for t in trades) / total if total > 0 else 0.0
    )
    avg_win = (gross_profit / len(wins_list)) if wins_list else 0.0
    avg_loss = (-gross_loss / len(losses_list)) if losses_list else 0.0

    sharpe = 0.0
    if len(daily_returns) >= 2:
        arr = np.asarray(daily_returns, dtype=float)
        std = float(np.std(arr, ddof=1))
        if std > 1e-9:
            sharpe = float(np.mean(arr) / std * math.sqrt(252.0))

    net_profit = final_equity - initial_equity
    ret_pct = (net_profit / initial_equity * 100.0) if initial_equity > 0 else 0.0

    return BacktestReport(
        mode=config.strategy_mode,
        atr_profile=config.atr_profile,
        symbol=config.symbol,
        bars_tested=bars_tested,
        sessions_tested=sessions_tested,
        initial_equity=round(initial_equity, 2),
        final_equity=round(final_equity, 2),
        net_profit_usd=round(net_profit, 2),
        total_return_pct=round(ret_pct, 2),
        total_trades=total,
        long_trades=long_cnt,
        short_trades=short_cnt,
        missed_limit_entries=missed_limits,
        wins=len(wins_list),
        losses=len(losses_list),
        win_rate_pct=round(win_rate, 2),
        profit_factor=pf,
        expectancy_r=round(expectancy_r, 3),
        avg_risk_pct_deployed=round(avg_risk_pct, 2),
        avg_win_usd=round(avg_win, 2),
        avg_loss_usd=round(avg_loss, 2),
        max_drawdown_pct=round(max_dd * 100.0, 2),
        sharpe_annualized=round(sharpe, 2),
        trades=trades,
    )


def _empty_report(config: BotConfig, initial_equity: float, bars: int) -> BacktestReport:
    return BacktestReport(
        mode=config.strategy_mode,
        atr_profile=config.atr_profile,
        symbol=config.symbol,
        bars_tested=bars,
        sessions_tested=0,
        initial_equity=initial_equity,
        final_equity=initial_equity,
        net_profit_usd=0.0,
        total_return_pct=0.0,
        total_trades=0,
        long_trades=0,
        short_trades=0,
        missed_limit_entries=0,
        wins=0,
        losses=0,
        win_rate_pct=0.0,
        profit_factor=0.0,
        expectancy_r=0.0,
        avg_risk_pct_deployed=0.0,
        avg_win_usd=0.0,
        avg_loss_usd=0.0,
        max_drawdown_pct=0.0,
        sharpe_annualized=0.0,
        trades=[],
    )
