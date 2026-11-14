"""Validated bot configuration with Paper-Trading lock, ATR profiles, and v2 Session-Regime settings."""

from __future__ import annotations

import os
from datetime import time
from enum import Enum
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"


class LiveTradingProhibitedError(RuntimeError):
    """Raised whenever configuration attempts to disable paper mode or target live endpoints."""


class StrategyMode(str, Enum):
    """Supported execution strategy modes."""

    ALPHA_MAX = "alpha_max"
    BASELINE = "baseline"


class ATRProfile(str, Enum):
    """Supported ATR bracket width profiles (both maintain a 1.5R reward-to-risk ratio)."""

    STANDARD = "standard"  # 1.50x ATR SL / 2.25x ATR TP (1.5R)
    WIDE = "wide"          # 2.00x ATR SL / 3.00x ATR TP (1.5R — >50% OOS WR on SPY & higher risk deployment)


class BotConfig(BaseModel):
    """Immutable runtime configuration for the intraday trading engine & backtester."""

    model_config = ConfigDict(frozen=True)

    # Credentials & Broker Safety Lock
    alpaca_api_key: str = Field(default="")
    alpaca_secret_key: str = Field(default="")
    alpaca_paper: bool = Field(default=True)
    alpaca_base_url: str = Field(default=PAPER_TRADING_BASE_URL)
    data_feed: Literal["iex", "sip"] = Field(default="iex")

    # Crash-Safe Local Persistence
    state_db_path: str = Field(default="data/bot_state.db")

    # Instrument & Timeframe
    symbol: str = Field(default="SPY")
    bar_timeframe_minutes: int = Field(default=5, ge=1, le=60)
    poll_interval_seconds: int = Field(default=60, ge=5, le=300)
    boundary_wakeup_offset_sec: float = Field(default=1.5, ge=0.2, le=15.0)
    bars_to_fetch: int = Field(default=650, ge=250, le=5000)
    lookback_calendar_days: int = Field(default=20, ge=7, le=180)

    # Indicator Parameters (Exact Prompt Core + EMA-50 Trend Confirmation)
    ema_length: int = Field(default=200, ge=20, le=500)
    ema_fast_length: int = Field(default=50, ge=5, le=199)
    stochrsi_length: int = Field(default=14, ge=2, le=100)
    stochrsi_rsi_length: int = Field(default=14, ge=2, le=100)
    stochrsi_k: int = Field(default=3, ge=1, le=20)
    stochrsi_d: int = Field(default=3, ge=1, le=20)
    atr_length: int = Field(default=14, ge=2, le=100)
    oversold_threshold: float = Field(default=20.0, gt=0.0, lt=50.0)
    overbought_threshold: float = Field(default=80.0, gt=50.0, lt=100.0)

    # Risk, ATR Profile & Marketable Limit Collar Parameters
    atr_profile: ATRProfile = Field(default=ATRProfile.STANDARD)
    risk_per_trade_pct: float = Field(default=0.01, gt=0.0, le=0.05)
    stop_loss_atr_mult: float = Field(default=1.5, gt=0.1, le=10.0)
    take_profit_atr_mult: float = Field(default=2.25, gt=0.1, le=20.0)
    buying_power_safety_buffer: float = Field(default=0.95, gt=0.1, le=1.0)
    max_notional_equity_mult: float = Field(default=3.80, gt=0.5, le=4.0)
    min_stop_distance_usd: float = Field(default=0.05, ge=0.02, le=5.0)
    limit_collar_min_usd: float = Field(default=0.03, ge=0.01, le=2.0)
    limit_collar_atr_fraction: float = Field(default=0.08, ge=0.01, le=0.50)
    stale_entry_cancel_seconds: int = Field(default=55, ge=10, le=300)

    # Strategy Mode & v2 Quant Session-Regime Enhancements
    strategy_mode: StrategyMode = Field(default=StrategyMode.ALPHA_MAX)
    ema_slope_lookback: int = Field(default=3, ge=1, le=20)
    min_atr_pct: float = Field(default=0.00035, ge=0.0, le=0.05)
    max_atr_pct: float = Field(default=0.0150, gt=0.0005, le=0.20)
    daily_max_drawdown_pct: float = Field(default=0.025, gt=0.002, le=0.20)
    cooldown_bars_after_stop: int = Field(default=2, ge=0, le=20)

    # NYSE/NASDAQ Intraday Session Governance (America/New_York)
    rth_open_et: time = Field(default=time(9, 30))
    rth_close_et: time = Field(default=time(16, 0))
    alpha_entry_start_et: time = Field(default=time(9, 40))
    alpha_entry_cutoff_et: time = Field(default=time(15, 0))
    alpha_skip_long_lunch_start_et: time = Field(default=time(11, 0))
    alpha_skip_long_lunch_end_et: time = Field(default=time(13, 0))
    alpha_short_earliest_et: time = Field(default=time(11, 0))
    baseline_entry_start_et: time = Field(default=time(9, 30))
    baseline_entry_cutoff_et: time = Field(default=time(15, 50))
    eod_flatten_et: time = Field(default=time(15, 55))

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, v: str) -> str:
        cleaned = v.strip().upper()
        if not cleaned or not cleaned.isalnum():
            raise ValueError(f"Invalid ticker symbol: {v!r}")
        return cleaned

    @field_validator("data_feed", mode="before")
    @classmethod
    def _normalize_feed(cls, v: str) -> str:
        return str(v).strip().lower()

    @model_validator(mode="after")
    def _enforce_paper_lock_and_warmup(self) -> "BotConfig":
        if not self.alpaca_paper:
            raise LiveTradingProhibitedError(
                "LIVE TRADING BLOCKED: alpaca_paper must be True. This bot is strictly locked to Alpaca Paper Trading."
            )
        normalized_url = self.alpaca_base_url.strip().rstrip("/")
        if normalized_url.endswith("/v2"):
            normalized_url = normalized_url[:-3]
        if normalized_url != PAPER_TRADING_BASE_URL:
            raise LiveTradingProhibitedError(
                f"LIVE TRADING BLOCKED: alpaca_base_url={self.alpaca_base_url!r} does not match "
                f"{PAPER_TRADING_BASE_URL!r}."
            )
        min_required_bars = self.ema_length + 50
        if self.bars_to_fetch < min_required_bars:
            raise ValueError(
                f"bars_to_fetch ({self.bars_to_fetch}) must be >= {min_required_bars} "
                f"so {self.ema_length}-EMA is mathematically converged and non-NaN."
            )
        if self.atr_profile == ATRProfile.WIDE and (
            self.stop_loss_atr_mult == 1.5 and self.take_profit_atr_mult == 2.25
        ):
            object.__setattr__(self, "stop_loss_atr_mult", 2.0)
            object.__setattr__(self, "take_profit_atr_mult", 3.0)
        return self

    @property
    def allow_short(self) -> bool:
        return self.strategy_mode == StrategyMode.ALPHA_MAX

    @property
    def use_marketable_limit_entry(self) -> bool:
        return self.strategy_mode == StrategyMode.ALPHA_MAX

    @property
    def entry_start_et(self) -> time:
        return (
            self.alpha_entry_start_et
            if self.strategy_mode == StrategyMode.ALPHA_MAX
            else self.baseline_entry_start_et
        )

    @property
    def entry_cutoff_et(self) -> time:
        return (
            self.alpha_entry_cutoff_et
            if self.strategy_mode == StrategyMode.ALPHA_MAX
            else self.baseline_entry_cutoff_et
        )

    def require_credentials(self) -> None:
        if not self.alpaca_api_key or not self.alpaca_secret_key:
            raise ValueError(
                "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY. Copy .env.example to .env and set your Alpaca Paper keys."
            )

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = None,
        *,
        mode_override: str | StrategyMode | None = None,
        symbol_override: str | None = None,
        atr_profile_override: str | ATRProfile | None = None,
    ) -> "BotConfig":
        """Load configuration from environment variables (and optional .env file)."""
        if env_file is not None:
            load_dotenv(dotenv_path=env_file, override=False)
        else:
            load_dotenv(override=False)

        raw_paper = os.getenv("ALPACA_PAPER", "true").strip().lower()
        if raw_paper in {"false", "0", "no", "live"}:
            raise LiveTradingProhibitedError(
                f"LIVE TRADING BLOCKED: ALPACA_PAPER={raw_paper!r} is prohibited."
            )

        raw_bars = int(os.getenv("BARS_TO_FETCH", "650"))
        ema_len = int(os.getenv("EMA_LENGTH", "200"))
        safe_bars = max(raw_bars, ema_len + 100)

        resolved_mode = (
            StrategyMode(mode_override)
            if mode_override is not None
            else StrategyMode(os.getenv("STRATEGY_MODE", "alpha_max").strip().lower())
        )
        resolved_profile = (
            ATRProfile(atr_profile_override)
            if atr_profile_override is not None
            else ATRProfile(os.getenv("ATR_PROFILE", "standard").strip().lower())
        )

        sl_mult = float(os.getenv("STOP_LOSS_ATR_MULT", "1.5"))
        tp_mult = float(os.getenv("TAKE_PROFIT_ATR_MULT", "2.25"))
        if resolved_profile == ATRProfile.WIDE and "STOP_LOSS_ATR_MULT" not in os.environ:
            sl_mult, tp_mult = 2.0, 3.0

        return cls(
            alpaca_api_key=os.getenv("ALPACA_API_KEY", "").strip(),
            alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", "").strip(),
            alpaca_paper=True,
            alpaca_base_url=os.getenv(
                "ALPACA_BASE_URL",
                os.getenv("ALPACA_PAPER_BASE_URL", PAPER_TRADING_BASE_URL),
            ).strip(),
            data_feed=os.getenv("ALPACA_DATA_FEED", "iex").strip().lower(),  # type: ignore[arg-type]
            state_db_path=os.getenv("STATE_DB_PATH", "data/bot_state.db").strip(),
            symbol=(symbol_override or os.getenv("TRADING_SYMBOL", "SPY")).strip().upper(),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            bars_to_fetch=safe_bars,
            ema_length=ema_len,
            atr_profile=resolved_profile,
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "0.01")),
            stop_loss_atr_mult=sl_mult,
            take_profit_atr_mult=tp_mult,
            daily_max_drawdown_pct=float(os.getenv("DAILY_MAX_DRAWDOWN_PCT", "0.025")),
            buying_power_safety_buffer=float(
                os.getenv("BUYING_POWER_SAFETY_BUFFER", "0.95")
            ),
            max_notional_equity_mult=float(
                os.getenv("MAX_NOTIONAL_EQUITY_MULT", "3.80")
            ),
            strategy_mode=resolved_mode,
        )
