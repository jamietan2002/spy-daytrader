"""Deterministic v2 intraday signal engine supporting Baseline and Session-Regime Alpha-Max."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import numpy as np
import pandas as pd

from spy_daytrader.config import BotConfig, StrategyMode


class SignalAction(str, Enum):
    """Discrete trading signal emitted on a closed 5-minute bar."""

    NONE = "NONE"
    BUY = "BUY"
    SELL_SHORT = "SELL_SHORT"


@dataclass(frozen=True)
class SignalDecision:
    """Full causal snapshot & decision reason for auditability and backtest parity."""

    action: SignalAction
    reason: str
    symbol: str
    mode: StrategyMode
    bar_open_et: datetime | None
    bar_close_et: datetime | None
    close: float
    ema_200: float
    ema_50: float
    ema_200_slope: float
    session_vwap: float
    stochrsi_k_prev: float
    stochrsi_d_prev: float
    stochrsi_k: float
    stochrsi_d: float
    atr_14: float
    atr_pct: float
    is_bull_candle: bool


def evaluate_signal(enriched_df: pd.DataFrame, config: BotConfig) -> SignalDecision:
    """
    Evaluate the most recently closed 5-minute bar (`iloc[-1]`) against `iloc[-2]`.

    Modes:
      - `BASELINE`: Exact prompt specification (Long-only, `Close > EMA_200`, `StochRSI` cross up
        from `< 20` on closed 5m bars).
      - `ALPHA_MAX`: Walk-forward validated v2 Session-Regime engine:
          * Entry horizon gate: `09:40 - 15:00 ET` (eliminates pre-15:55 EOD horizon truncation)
          * Longs: skips `11:00 - 13:00 ET` NYC lunch chop, requires `Close > EMA_200`,
            `EMA_50 > EMA_200`, `Close >= Session_VWAP`, and bullish reversal bar (`Close > Open`).
          * Shorts: active `>= 11:00 ET` (skips morning short-squeeze window), requires
            `Close < EMA_200`, declining `EMA_200` (`slope < 0`), and `StochRSI` cross down from `> 80`.
    """
    min_bars = config.ema_length + config.ema_slope_lookback + 2
    if enriched_df is None or len(enriched_df) < min_bars:
        count = 0 if enriched_df is None else len(enriched_df)
        return _empty_decision(
            config,
            reason=f"INSUFFICIENT_BARS ({count} < {min_bars} required for converged {config.ema_length}-EMA)",
        )

    prev = enriched_df.iloc[-2]
    curr = enriched_df.iloc[-1]

    bar_open_ts = pd.Timestamp(curr.name).to_pydatetime()
    bar_close_ts = (
        pd.Timestamp(curr.name) + pd.Timedelta(minutes=config.bar_timeframe_minutes)
    ).to_pydatetime()

    open_px = float(curr.get("open", curr["close"]))
    close_px = float(curr["close"])
    ema_200 = float(curr["ema_200"])
    ema_50 = float(curr["ema_50"]) if "ema_50" in curr and pd.notna(curr["ema_50"]) else ema_200
    ema_slope = float(curr["ema_200_slope"])
    vwap = float(curr["session_vwap"])
    k_prev = float(prev["stochrsi_k"])
    d_prev = float(prev["stochrsi_d"])
    k_curr = float(curr["stochrsi_k"])
    d_curr = float(curr["stochrsi_d"])
    atr_14 = float(curr["atr_14"])
    atr_pct = float(curr["atr_pct"])
    is_bull = bool(curr["is_bull_candle"]) if "is_bull_candle" in curr else (close_px > open_px)

    required_vals = (
        close_px,
        ema_200,
        ema_50,
        vwap,
        k_prev,
        d_prev,
        k_curr,
        d_curr,
        atr_14,
        ema_slope,
        atr_pct,
    )
    if any(pd.isna(v) or not np.isfinite(float(v)) for v in required_vals):
        return _empty_decision(
            config,
            reason="INDICATOR_WARMUP_NAN",
            bar_open_et=bar_open_ts,
            bar_close_et=bar_close_ts,
        )

    def _build(action: SignalAction, reason: str) -> SignalDecision:
        return SignalDecision(
            action=action,
            reason=reason,
            symbol=config.symbol,
            mode=config.strategy_mode,
            bar_open_et=bar_open_ts,
            bar_close_et=bar_close_ts,
            close=close_px,
            ema_200=ema_200,
            ema_50=ema_50,
            ema_200_slope=ema_slope,
            session_vwap=vwap,
            stochrsi_k_prev=k_prev,
            stochrsi_d_prev=d_prev,
            stochrsi_k=k_curr,
            stochrsi_d=d_curr,
            atr_14=atr_14,
            atr_pct=atr_pct,
            is_bull_candle=is_bull,
        )

    if atr_14 <= 0.0 or close_px <= 0.0:
        return _build(SignalAction.NONE, "INVALID_PRICE_OR_ATR")

    exec_time_et = bar_close_ts.time()
    if exec_time_et < config.entry_start_et or exec_time_et > config.entry_cutoff_et:
        return _build(
            SignalAction.NONE,
            f"OUTSIDE_ENTRY_WINDOW ({exec_time_et.strftime('%H:%M')} ET not in "
            f"{config.entry_start_et.strftime('%H:%M')}-{config.entry_cutoff_et.strftime('%H:%M')} ET)",
        )

    if config.strategy_mode == StrategyMode.ALPHA_MAX:
        if atr_pct < config.min_atr_pct:
            return _build(
                SignalAction.NONE,
                f"LOW_VOLATILITY_CHOP (ATR%={atr_pct:.4%} < {config.min_atr_pct:.4%})",
            )
        if atr_pct > config.max_atr_pct:
            return _build(
                SignalAction.NONE,
                f"EXTREME_VOLATILITY_SPIKE (ATR%={atr_pct:.4%} > {config.max_atr_pct:.4%})",
            )

    bullish_stoch_cross = (
        k_prev < config.oversold_threshold
        and k_prev <= d_prev
        and k_curr > d_curr
        and k_curr < 60.0
    )
    bearish_stoch_cross = (
        k_prev > config.overbought_threshold
        and k_prev >= d_prev
        and k_curr < d_curr
        and k_curr > 40.0
    )

    # 1. LONG Pullback Evaluation
    if close_px > ema_200 and bullish_stoch_cross:
        if config.strategy_mode == StrategyMode.ALPHA_MAX:
            if (
                config.alpha_skip_long_lunch_start_et
                <= exec_time_et
                < config.alpha_skip_long_lunch_end_et
            ):
                return _build(
                    SignalAction.NONE,
                    f"LONG_VETO_LUNCH_CHOP ({exec_time_et.strftime('%H:%M')} ET in 11:00-13:00 ET zone)",
                )
            if close_px < vwap:
                return _build(
                    SignalAction.NONE,
                    f"LONG_VETO_BELOW_VWAP (close={close_px:.2f} < vwap={vwap:.2f})",
                )
            if ema_50 <= ema_200:
                return _build(
                    SignalAction.NONE,
                    f"LONG_VETO_EMA50_BELOW_EMA200 (EMA50={ema_50:.2f} <= EMA200={ema_200:.2f})",
                )
            if not is_bull:
                return _build(
                    SignalAction.NONE,
                    "LONG_VETO_NON_BULLISH_CANDLE (close <= open)",
                )

        return _build(
            SignalAction.BUY,
            f"LONG_SIGNAL (close={close_px:.2f} > EMA200={ema_200:.2f}, "
            f"StochRSI K {k_prev:.1f}->{k_curr:.1f} crossed above D {d_prev:.1f}->{d_curr:.1f})",
        )

    # 2. Symmetric SHORT Pullback Evaluation (Alpha-Max Mode)
    if config.allow_short and close_px < ema_200 and bearish_stoch_cross:
        if exec_time_et < config.alpha_short_earliest_et:
            return _build(
                SignalAction.NONE,
                f"SHORT_VETO_MORNING_SQUEEZE ({exec_time_et.strftime('%H:%M')} ET < "
                f"{config.alpha_short_earliest_et.strftime('%H:%M')} ET)",
            )
        if ema_slope >= 0.0:
            return _build(
                SignalAction.NONE,
                f"SHORT_VETO_NON_DECLINING_EMA200 (slope={ema_slope:.4f} >= 0)",
            )
        return _build(
            SignalAction.SELL_SHORT,
            f"SHORT_SIGNAL (close={close_px:.2f} < EMA200={ema_200:.2f}, "
            f"StochRSI K {k_prev:.1f}->{k_curr:.1f} crossed below D {d_prev:.1f}->{d_curr:.1f})",
        )

    return _build(SignalAction.NONE, "NO_SETUP")


def _empty_decision(
    config: BotConfig,
    *,
    reason: str,
    bar_open_et: datetime | None = None,
    bar_close_et: datetime | None = None,
) -> SignalDecision:
    return SignalDecision(
        action=SignalAction.NONE,
        reason=reason,
        symbol=config.symbol,
        mode=config.strategy_mode,
        bar_open_et=bar_open_et,
        bar_close_et=bar_close_et,
        close=float("nan"),
        ema_200=float("nan"),
        ema_50=float("nan"),
        ema_200_slope=float("nan"),
        session_vwap=float("nan"),
        stochrsi_k_prev=float("nan"),
        stochrsi_d_prev=float("nan"),
        stochrsi_k=float("nan"),
        stochrsi_d=float("nan"),
        atr_14=float("nan"),
        atr_pct=float("nan"),
        is_bull_candle=False,
    )
