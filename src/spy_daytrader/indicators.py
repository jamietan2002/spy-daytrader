"""Causal technical indicator pipeline powered by pandas-ta."""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import pandas_ta as ta
except ImportError:  # pragma: no cover
    import pandas_ta_classic as ta  # type: ignore[no-redef]

from spy_daytrader.config import BotConfig

INDICATOR_COLUMNS = (
    "ema_200",
    "ema_50",
    "stochrsi_k",
    "stochrsi_d",
    "atr_14",
    "session_vwap",
    "ema_200_slope",
    "atr_pct",
    "is_bull_candle",
    "is_bear_candle",
)


def compute_indicators(bars_df: pd.DataFrame, config: BotConfig) -> pd.DataFrame:
    """
    Enrich closed 5m RTH bars with strictly causal technical indicators:
      - 200-period EMA (`ema_200`) and 50-period EMA (`ema_50`)
      - Stochastic RSI (14, 14, 3, 3) `%K` (`stochrsi_k`) and `%D` (`stochrsi_d`)
      - 14-period Wilder ATR (`atr_14`)
      - Intraday NY-Session anchored VWAP (`session_vwap`, resets at 09:30 ET daily)
      - EMA-200 directional slope (`ema_200_slope`), `atr_pct`, and candle polarity
    """
    if bars_df is None or bars_df.empty:
        return pd.DataFrame()

    df = bars_df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 1. 200-period & 50-period Exponential Moving Averages
    ema_200 = ta.ema(df["close"], length=config.ema_length)
    ema_50 = ta.ema(df["close"], length=config.ema_fast_length)
    df["ema_200"] = ema_200 if ema_200 is not None else np.nan
    df["ema_50"] = ema_50 if ema_50 is not None else np.nan

    # 2. Stochastic RSI (14, 14, 3, 3)
    stoch_df = ta.stochrsi(
        df["close"],
        length=config.stochrsi_length,
        rsi_length=config.stochrsi_rsi_length,
        k=config.stochrsi_k,
        d=config.stochrsi_d,
    )
    if stoch_df is not None and not stoch_df.empty:
        k_cols = [c for c in stoch_df.columns if str(c).upper().startswith("STOCHRSIK_")]
        d_cols = [c for c in stoch_df.columns if str(c).upper().startswith("STOCHRSID_")]
        if not k_cols or not d_cols:
            raise RuntimeError(
                f"Unexpected pandas-ta stochrsi columns: {list(stoch_df.columns)}"
            )
        df["stochrsi_k"] = stoch_df[k_cols[0]]
        df["stochrsi_d"] = stoch_df[d_cols[0]]
    else:
        df["stochrsi_k"] = np.nan
        df["stochrsi_d"] = np.nan

    # 3. 14-period Average True Range (Wilder RMA smoothing)
    atr_series = ta.atr(
        df["high"],
        df["low"],
        df["close"],
        length=config.atr_length,
    )
    df["atr_14"] = atr_series if atr_series is not None else np.nan

    # 4. Causal Intraday Session VWAP (strictly resets per NY trading date at 09:30 ET)
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    clean_vol = df["volume"].clip(lower=0.0).fillna(0.0)
    session_dates = pd.Series(df.index.date, index=df.index)
    cum_pv = (typical_price * clean_vol).groupby(session_dates).cumsum()
    cum_vol = clean_vol.groupby(session_dates).cumsum()
    df["session_vwap"] = np.where(cum_vol > 0.0, cum_pv / cum_vol, df["close"])

    # 5. Regime, Volatility & Candle Confirmation Diagnostics
    df["ema_200_slope"] = df["ema_200"] - df["ema_200"].shift(config.ema_slope_lookback)
    df["atr_pct"] = np.where(df["close"] > 0.0, df["atr_14"] / df["close"], np.nan)
    df["is_bull_candle"] = df["close"] > df["open"]
    df["is_bear_candle"] = df["close"] < df["open"]

    return df
