"""ATR-based 1% equity risk sizing, marketable limit collars, BP caps, and bot-scoped circuit breakers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from spy_daytrader.config import BotConfig, StrategyMode
from spy_daytrader.state_store import BOT_ORDER_PREFIX


@dataclass(frozen=True)
class AccountSnapshot:
    """Normalized account state extracted from Alpaca TradingClient.get_account()."""

    equity: float
    last_equity: float
    buying_power: float
    cash: float
    trading_blocked: bool = False
    account_blocked: bool = False
    shorting_enabled: bool = True

    @property
    def daily_pnl_usd(self) -> float:
        return self.equity - self.last_equity

    @property
    def daily_pnl_pct(self) -> float:
        if self.last_equity <= 0.0:
            return 0.0
        return (self.equity - self.last_equity) / self.last_equity


@dataclass(frozen=True)
class BracketOrderPlan:
    """Validated, tick-rounded integer-share bracket order plan (Market or Marketable Limit)."""

    approved: bool
    veto_reason: str
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"]
    qty: int
    reference_price: float
    limit_price: float | None
    stop_loss_price: float
    take_profit_price: float
    atr_14: float
    risk_budget_usd: float
    actual_risk_usd: float
    notional_usd: float
    reward_to_risk: float
    capped_by_buying_power: bool
    client_order_id: str


def build_client_order_id(
    symbol: str,
    mode: StrategyMode,
    side: Literal["BUY", "SELL"],
    bar_close_et: datetime,
) -> str:
    """Build a deterministic <=48-char bot-prefixed (`spydt-*`) idempotency key per closed 5m bar."""
    mode_tag = "amx" if mode == StrategyMode.ALPHA_MAX else "bsl"
    side_tag = "lng" if side == "BUY" else "sht"
    ts_tag = bar_close_et.strftime("%Y%m%dT%H%M")
    return f"{BOT_ORDER_PREFIX}{symbol.lower()}-{mode_tag}-{side_tag}-{ts_tag}"[:48]


def calculate_bracket_order_plan(
    *,
    account: AccountSnapshot,
    side: Literal["BUY", "SELL"],
    reference_price: float,
    atr_14: float,
    bar_close_et: datetime,
    config: BotConfig,
    bot_session_realized_pnl_usd: float = 0.0,
) -> BracketOrderPlan:
    """
    Compute whole-share position size risking 1% of account equity with:
      - Stop-Loss at `stop_loss_atr_mult * ATR` (1.5x default, 2.0x in `wide` profile)
      - Take-Profit at `take_profit_atr_mult * ATR` (2.25x default, 3.0x in `wide` profile = 1.5R)
      - Marketable Limit collar (`ask/bid +- max($0.03, 0.08 * ATR)`) in `alpha_max`, Market in `baseline`
      - Hard cap against available broker buying power & max notional leverage
      - Bot-scoped AND account-level daily drawdown circuit breakers
    """
    cid = build_client_order_id(config.symbol, config.strategy_mode, side, bar_close_et)
    ord_type: Literal["MARKET", "LIMIT"] = (
        "LIMIT" if config.use_marketable_limit_entry else "MARKET"
    )

    def _veto(reason: str) -> BracketOrderPlan:
        return BracketOrderPlan(
            approved=False,
            veto_reason=reason,
            symbol=config.symbol,
            side=side,
            order_type=ord_type,
            qty=0,
            reference_price=round(max(reference_price, 0.0), 2),
            limit_price=None,
            stop_loss_price=0.0,
            take_profit_price=0.0,
            atr_14=atr_14,
            risk_budget_usd=0.0,
            actual_risk_usd=0.0,
            notional_usd=0.0,
            reward_to_risk=0.0,
            capped_by_buying_power=False,
            client_order_id=cid,
        )

    if account.trading_blocked or account.account_blocked:
        return _veto("ACCOUNT_OR_TRADING_BLOCKED")

    if side == "SELL" and not account.shorting_enabled:
        return _veto("SHORTING_DISABLED_ON_ACCOUNT")

    if account.equity <= 0.0 or reference_price <= 0.0 or atr_14 <= 0.0:
        return _veto("NON_POSITIVE_EQUITY_PRICE_OR_ATR")

    # 1. Bot-scoped daily drawdown circuit breaker (from SQLite ledger)
    bot_dd_pct = bot_session_realized_pnl_usd / account.equity
    if bot_dd_pct <= -config.daily_max_drawdown_pct:
        return _veto(
            f"BOT_DAILY_CIRCUIT_BREAKER_HIT (bot_pnl={bot_dd_pct:.2%} <= "
            f"-{config.daily_max_drawdown_pct:.2%})"
        )

    # 2. Account-wide daily drawdown circuit breaker
    if account.daily_pnl_pct <= -config.daily_max_drawdown_pct:
        return _veto(
            f"DAILY_CIRCUIT_BREAKER_HIT (daily_pnl={account.daily_pnl_pct:.2%} <= "
            f"-{config.daily_max_drawdown_pct:.2%})"
        )

    entry_px = round(reference_price, 2)
    collar = round(
        max(config.limit_collar_min_usd, config.limit_collar_atr_fraction * atr_14), 2
    )
    limit_px: float | None = None
    if ord_type == "LIMIT":
        limit_px = (
            round(entry_px + collar, 2)
            if side == "BUY"
            else round(max(0.05, entry_px - collar), 2)
        )

    raw_stop_dist = max(config.stop_loss_atr_mult * atr_14, config.min_stop_distance_usd)
    raw_tp_dist = max(
        config.take_profit_atr_mult * atr_14,
        raw_stop_dist * (config.take_profit_atr_mult / config.stop_loss_atr_mult),
    )

    if side == "BUY":
        stop_px = round(entry_px - raw_stop_dist, 2)
        tp_px = round(entry_px + raw_tp_dist, 2)
        min_tp_anchor = limit_px if limit_px is not None else entry_px
        if stop_px > round(entry_px - 0.02, 2):
            stop_px = round(entry_px - 0.02, 2)
        if tp_px < round(min_tp_anchor + 0.02, 2):
            tp_px = round(min_tp_anchor + 0.02, 2)
        per_share_risk = round(entry_px - stop_px, 4)
        per_share_reward = round(tp_px - entry_px, 4)
    else:
        stop_px = round(entry_px + raw_stop_dist, 2)
        tp_px = round(entry_px - raw_tp_dist, 2)
        max_tp_anchor = limit_px if limit_px is not None else entry_px
        if stop_px < round(entry_px + 0.02, 2):
            stop_px = round(entry_px + 0.02, 2)
        if tp_px > round(max_tp_anchor - 0.02, 2):
            tp_px = round(max_tp_anchor - 0.02, 2)
        per_share_risk = round(stop_px - entry_px, 4)
        per_share_reward = round(entry_px - tp_px, 4)

    if stop_px <= 0.0 or tp_px <= 0.0 or per_share_risk <= 0.0:
        return _veto("INVALID_ROUNDED_BRACKET_PRICES")

    risk_budget_usd = account.equity * config.risk_per_trade_pct
    qty_by_risk = math.floor(risk_budget_usd / per_share_risk)

    sizing_px = limit_px if (limit_px is not None and side == "BUY") else entry_px
    broker_bp_cap = max(account.buying_power, 0.0) * config.buying_power_safety_buffer
    leverage_cap = account.equity * config.max_notional_equity_mult
    effective_notional_cap = min(broker_bp_cap, leverage_cap)
    qty_by_bp = math.floor(effective_notional_cap / sizing_px)

    qty = min(qty_by_risk, qty_by_bp)
    if qty < 1:
        return _veto(
            f"POSITION_SIZE_ZERO (qty_by_risk={qty_by_risk}, qty_by_bp={qty_by_bp}, "
            f"entry={entry_px:.2f}, bp={account.buying_power:.2f})"
        )

    capped_by_bp = qty_by_bp < qty_by_risk
    actual_risk_usd = round(qty * per_share_risk, 2)
    notional_usd = round(qty * entry_px, 2)
    rr_ratio = round(per_share_reward / per_share_risk, 3)

    return BracketOrderPlan(
        approved=True,
        veto_reason="",
        symbol=config.symbol,
        side=side,
        order_type=ord_type,
        qty=int(qty),
        reference_price=entry_px,
        limit_price=limit_px,
        stop_loss_price=stop_px,
        take_profit_price=tp_px,
        atr_14=atr_14,
        risk_budget_usd=round(risk_budget_usd, 2),
        actual_risk_usd=actual_risk_usd,
        notional_usd=notional_usd,
        reward_to_risk=rr_ratio,
        capped_by_buying_power=capped_by_bp,
        client_order_id=cid,
    )
