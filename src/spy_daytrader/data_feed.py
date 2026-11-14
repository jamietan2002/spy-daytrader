"""Alpaca historical 5-minute bar & live NBBO quote feed with RTH & closed-bar guarantees."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from spy_daytrader.config import BotConfig
from spy_daytrader.logging_utils import get_logger

NY_TZ = ZoneInfo("America/New_York")
REQUIRED_OHLCV_COLS = ("open", "high", "low", "close", "volume")


def normalize_and_filter_rth_closed_bars(
    raw_df: pd.DataFrame,
    *,
    symbol: str,
    bar_minutes: int,
    now_utc: datetime,
    rth_open_et: time = time(9, 30),
    rth_close_et: time = time(16, 0),
    tail_bars: int | None = 650,
) -> pd.DataFrame:
    """
    Normalize an Alpaca bars DataFrame to ascending America/New_York DatetimeIndex,
    filter strictly to Regular Trading Hours (09:30-16:00 ET, Mon-Fri), and drop any
    incomplete/still-forming bar whose close timestamp exceeds `now_utc`.
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=list(REQUIRED_OHLCV_COLS))

    df = raw_df.copy()

    # Unwrap MultiIndex (symbol, timestamp) returned by alpaca-py BarSet.df
    if isinstance(df.index, pd.MultiIndex):
        if "symbol" in df.index.names:
            symbols = df.index.get_level_values("symbol")
            if symbol in symbols:
                df = df.xs(symbol, level="symbol")
            else:
                return pd.DataFrame(columns=list(REQUIRED_OHLCV_COLS))
        else:
            df = df.droplevel(0)

    if not isinstance(df.index, pd.DatetimeIndex):
        if "timestamp" in df.columns:
            df = df.set_index("timestamp")
        else:
            raise ValueError("Bars DataFrame must have a DatetimeIndex or 'timestamp' column.")

    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    idx_et = idx.tz_convert(NY_TZ)
    df.index = idx_et
    df.index.name = "timestamp"

    df.columns = [str(c).lower() for c in df.columns]
    missing = [c for c in REQUIRED_OHLCV_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"Bars DataFrame missing required OHLCV columns: {missing}")

    # Deduplicate & sort strictly chronologically
    df = df[~df.index.duplicated(keep="last")].sort_index()

    # Filter Mon-Fri Regular Trading Hours (Alpaca stamps bars by bar-open time: 09:30 .. 15:55 ET)
    is_weekday = df.index.day_of_week < 5
    bar_times = df.index.time
    is_rth = (bar_times >= rth_open_et) & (bar_times < rth_close_et)
    df = df.loc[is_weekday & is_rth]

    if df.empty:
        return df

    # Drop unclosed / still-forming 5-minute candle: a bar opened at T closes at T + bar_minutes
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_et = now_utc.astimezone(NY_TZ)
    bar_close_times = df.index + pd.Timedelta(minutes=bar_minutes)
    df = df.loc[bar_close_times <= now_et]

    if tail_bars is not None and len(df) > tail_bars:
        df = df.tail(tail_bars)

    return df.copy()


class MarketDataFeed:
    """Resilient wrapper around Alpaca's StockHistoricalDataClient."""

    def __init__(
        self,
        config: BotConfig,
        client: StockHistoricalDataClient | None = None,
    ) -> None:
        self._config = config
        self._logger = get_logger("spy_daytrader.data")
        if client is not None:
            self._client = client
        else:
            config.require_credentials()
            self._client = StockHistoricalDataClient(
                api_key=config.alpaca_api_key,
                secret_key=config.alpaca_secret_key,
            )

    def _preferred_feed(self) -> DataFeed:
        return DataFeed.SIP if self._config.data_feed == "sip" else DataFeed.IEX

    def fetch_closed_bars(
        self,
        *,
        now_utc: datetime | None = None,
        lookback_days: int | None = None,
        tail_bars: int | None = None,
    ) -> pd.DataFrame:
        """Fetch converged historical RTH closed 5-minute bars up to `now_utc`."""
        effective_now = now_utc or datetime.now(timezone.utc)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)

        days = lookback_days or self._config.lookback_calendar_days
        start_utc = effective_now - timedelta(days=days)
        limit_tail = self._config.bars_to_fetch if tail_bars is None else tail_bars

        feed = self._preferred_feed()
        raw_df = self._request_bars_df(start_utc=start_utc, end_utc=effective_now, feed=feed)

        return normalize_and_filter_rth_closed_bars(
            raw_df,
            symbol=self._config.symbol,
            bar_minutes=self._config.bar_timeframe_minutes,
            now_utc=effective_now,
            rth_open_et=self._config.rth_open_et,
            rth_close_et=self._config.rth_close_et,
            tail_bars=limit_tail if limit_tail > 0 else None,
        )

    def _request_bars_df(
        self,
        *,
        start_utc: datetime,
        end_utc: datetime,
        feed: DataFeed,
    ) -> pd.DataFrame:
        tf = TimeFrame(self._config.bar_timeframe_minutes, TimeFrameUnit.Minute)
        req = StockBarsRequest(
            symbol_or_symbols=self._config.symbol,
            timeframe=tf,
            start=start_utc,
            end=end_utc,
            adjustment=Adjustment.ALL,
            feed=feed,
        )
        try:
            barset = self._client.get_stock_bars(req)
            return getattr(barset, "df", pd.DataFrame())
        except APIError as exc:
            if feed == DataFeed.SIP and (
                "subscription" in str(exc).lower() or "sip" in str(exc).lower()
            ):
                self._logger.warning(
                    "SIP feed not entitled on current account; automatically falling back to IEX feed."
                )
                fallback_req = StockBarsRequest(
                    symbol_or_symbols=self._config.symbol,
                    timeframe=tf,
                    start=start_utc,
                    end=end_utc,
                    adjustment=Adjustment.ALL,
                    feed=DataFeed.IEX,
                )
                barset = self._client.get_stock_bars(fallback_req)
                return getattr(barset, "df", pd.DataFrame())
            raise

    def fetch_executable_quote_price(
        self,
        side: Literal["BUY", "SELL"],
        *,
        fallback_close: float,
    ) -> float:
        """
        Fetch live NBBO ask (for BUY) or bid (for SELL) to anchor bracket SL/TP cleanly.
        Falls back to `fallback_close` if market quote is unavailable, wide, or stale.
        """
        feed = self._preferred_feed()
        try:
            req = StockLatestQuoteRequest(
                symbol_or_symbols=self._config.symbol,
                feed=feed,
            )
            quotes = self._client.get_stock_latest_quote(req)
            quote = quotes.get(self._config.symbol) if isinstance(quotes, dict) else None
            if quote is None:
                return fallback_close

            ask = float(getattr(quote, "ask_price", 0.0) or 0.0)
            bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
            if ask <= 0.0 or bid <= 0.0 or ask < bid:
                return fallback_close

            # Sanity-check spread (< 0.4%) and proximity to last bar close (< 0.6%)
            spread_pct = (ask - bid) / bid
            candidate = ask if side == "BUY" else bid
            deviation_pct = abs(candidate - fallback_close) / fallback_close
            if spread_pct <= 0.004 and deviation_pct <= 0.006:
                return round(candidate, 2)
        except Exception as exc:
            self._logger.debug("Quote lookup fallback to bar close (%.2f): %s", fallback_close, exc)

        return round(fallback_close, 2)
