"""Idempotent Alpaca Paper OMS adapter with spydt-* isolation, limit brackets, stale cancel, and EOD flatten."""

from __future__ import annotations

import time as time_mod
from dataclasses import dataclass
from datetime import date, datetime, timezone

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from spy_daytrader.config import BotConfig, LiveTradingProhibitedError
from spy_daytrader.logging_utils import get_logger
from spy_daytrader.risk import AccountSnapshot, BracketOrderPlan
from spy_daytrader.state_store import BOT_ORDER_PREFIX, SQLiteStateLedger


@dataclass(frozen=True)
class MarketClockState:
    """Current NYSE/NASDAQ session clock from Alpaca."""

    is_open: bool
    timestamp: datetime
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class OrderSubmissionReceipt:
    """Result of submitting a bracket order to Alpaca Paper OMS."""

    submitted: bool
    order_id: str
    client_order_id: str
    status: str
    message: str


class AlpacaPaperBroker:
    """Paper-locked brokerage gateway with bot-prefix isolation, stale-entry cleanup, and EOD flatten."""

    def __init__(
        self,
        config: BotConfig,
        trading_client: TradingClient | None = None,
        ledger: SQLiteStateLedger | None = None,
    ) -> None:
        if not config.alpaca_paper:
            raise LiveTradingProhibitedError("AlpacaPaperBroker refuses non-paper BotConfig.")

        self._config = config
        self._logger = get_logger("spy_daytrader.broker")
        self._ledger = ledger

        if trading_client is not None:
            self._client = trading_client
        else:
            config.require_credentials()
            self._client = TradingClient(
                api_key=config.alpaca_api_key,
                secret_key=config.alpaca_secret_key,
                paper=True,
                url_override=config.alpaca_base_url,
            )

    def get_account_snapshot(self) -> AccountSnapshot:
        acct = self._client.get_account()
        return AccountSnapshot(
            equity=float(getattr(acct, "equity", 0.0) or 0.0),
            last_equity=float(getattr(acct, "last_equity", 0.0) or 0.0),
            buying_power=float(getattr(acct, "buying_power", 0.0) or 0.0),
            cash=float(getattr(acct, "cash", 0.0) or 0.0),
            trading_blocked=bool(getattr(acct, "trading_blocked", False)),
            account_blocked=bool(getattr(acct, "account_blocked", False)),
            shorting_enabled=bool(getattr(acct, "shorting_enabled", True)),
        )

    def get_market_clock(self) -> MarketClockState:
        clock = self._client.get_clock()
        return MarketClockState(
            is_open=bool(clock.is_open),
            timestamp=clock.timestamp,
            next_open=clock.next_open,
            next_close=clock.next_close,
        )

    def get_open_symbol_position_qty(self, symbol: str | None = None) -> float:
        target = (symbol or self._config.symbol).upper()
        positions = self._client.get_all_positions()
        for pos in positions:
            if str(getattr(pos, "symbol", "")).upper() == target:
                qty = float(getattr(pos, "qty", 0.0) or 0.0)
                if qty != 0.0:
                    return qty
        return 0.0

    def get_open_symbol_orders(self, symbol: str | None = None) -> list[object]:
        target = (symbol or self._config.symbol).upper()
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[target])
        return list(self._client.get_orders(filter=req))

    def cancel_stale_unfilled_bot_entries(
        self,
        symbol: str | None = None,
        *,
        now_utc: datetime | None = None,
    ) -> list[str]:
        """
        Auto-cancel unfilled parent Marketable Limit entry orders (`spydt-*`) older than
        `stale_entry_cancel_seconds` so the bot never chases stale prices minutes later.
        Never cancels partially filled orders or child TP/SL protection legs.
        """
        target = (symbol or self._config.symbol).upper()
        effective_now = now_utc or datetime.now(timezone.utc)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)

        cancelled: list[str] = []
        for order in self.get_open_symbol_orders(target):
            cid = str(getattr(order, "client_order_id", "") or "")
            if not cid.startswith(BOT_ORDER_PREFIX):
                continue
            filled_qty = float(getattr(order, "filled_qty", 0.0) or 0.0)
            if filled_qty > 0.0:
                continue
            submitted_at = getattr(order, "submitted_at", None) or getattr(order, "created_at", None)
            if not isinstance(submitted_at, datetime):
                continue
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=timezone.utc)

            age_sec = (effective_now - submitted_at).total_seconds()
            if age_sec >= self._config.stale_entry_cancel_seconds:
                oid = getattr(order, "id", None)
                if oid is not None:
                    try:
                        self._client.cancel_order_by_id(oid)
                        cancelled.append(str(oid))
                        if self._ledger is not None:
                            self._ledger.update_order_status(cid, "CANCELLED_STALE")
                        self._logger.info(
                            "Cancelled stale unfilled entry order %s (%s, age=%.1fs)",
                            oid,
                            cid,
                            age_sec,
                        )
                    except APIError as exc:
                        self._logger.warning("Failed cancelling stale entry %s: %s", oid, exc)
        return cancelled

    def has_open_exposure(self, symbol: str | None = None) -> tuple[bool, str]:
        """
        Check BOTH open positions AND open/working orders for `symbol`.
        Prevents duplicate order submission while a prior entry order is still working.
        """
        target = (symbol or self._config.symbol).upper()
        pos_qty = self.get_open_symbol_position_qty(target)
        open_orders = self.get_open_symbol_orders(target)

        if pos_qty != 0.0:
            return True, f"OPEN_POSITION ({target} qty={pos_qty:g}, open_orders={len(open_orders)})"
        if open_orders:
            first_id = getattr(open_orders[0], "id", "unknown")
            first_status = getattr(open_orders[0], "status", "open")
            return (
                True,
                f"WORKING_ORDER_PENDING ({target} count={len(open_orders)}, first={first_id}:{first_status})",
            )
        return False, "FLAT"

    def submit_bracket_order(
        self,
        plan: BracketOrderPlan,
        *,
        session_date: date | None = None,
    ) -> OrderSubmissionReceipt:
        """Submit an integer-share DAY bracket order (Marketable Limit or Market) with SL & TP legs."""
        if not plan.approved or plan.qty < 1:
            return OrderSubmissionReceipt(
                submitted=False,
                order_id="",
                client_order_id=plan.client_order_id,
                status="VETOED",
                message=plan.veto_reason or "Order plan not approved.",
            )

        order_side = OrderSide.BUY if plan.side == "BUY" else OrderSide.SELL
        tp_req = TakeProfitRequest(limit_price=plan.take_profit_price)
        sl_req = StopLossRequest(stop_price=plan.stop_loss_price)

        if plan.order_type == "LIMIT" and plan.limit_price is not None:
            req: MarketOrderRequest | LimitOrderRequest = LimitOrderRequest(
                symbol=plan.symbol,
                qty=plan.qty,
                side=order_side,
                limit_price=plan.limit_price,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit=tp_req,
                stop_loss=sl_req,
                client_order_id=plan.client_order_id,
            )
        else:
            req = MarketOrderRequest(
                symbol=plan.symbol,
                qty=plan.qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit=tp_req,
                stop_loss=sl_req,
                client_order_id=plan.client_order_id,
            )

        try:
            order = self._client.submit_order(order_data=req)
            order_id = str(getattr(order, "id", ""))
            status = str(getattr(order, "status", "submitted"))
            if self._ledger is not None:
                self._ledger.record_order_submission(
                    client_order_id=plan.client_order_id,
                    broker_order_id=order_id,
                    symbol=plan.symbol,
                    mode=self._config.strategy_mode.value,
                    side=plan.side,
                    order_type=plan.order_type,
                    qty=plan.qty,
                    reference_price=plan.reference_price,
                    limit_price=plan.limit_price,
                    stop_loss_price=plan.stop_loss_price,
                    take_profit_price=plan.take_profit_price,
                    status=status,
                    session_date=session_date or datetime.now(timezone.utc).date(),
                )
            self._logger.info(
                "SUBMITTED %s BRACKET %s %d %s | ref=%.2f lim=%s SL=%.2f TP=%.2f | risk=$%.2f notional=$%.2f | id=%s",
                plan.order_type,
                plan.side,
                plan.qty,
                plan.symbol,
                plan.reference_price,
                f"{plan.limit_price:.2f}" if plan.limit_price is not None else "MKT",
                plan.stop_loss_price,
                plan.take_profit_price,
                plan.actual_risk_usd,
                plan.notional_usd,
                order_id,
            )
            return OrderSubmissionReceipt(
                submitted=True,
                order_id=order_id,
                client_order_id=plan.client_order_id,
                status=status,
                message="OK",
            )
        except APIError as exc:
            msg = str(exc)
            if "client_order_id" in msg.lower() and "unique" in msg.lower():
                self._logger.warning(
                    "Idempotency guard prevented duplicate order for client_order_id=%s",
                    plan.client_order_id,
                )
                return OrderSubmissionReceipt(
                    submitted=False,
                    order_id="",
                    client_order_id=plan.client_order_id,
                    status="DUPLICATE_SKIPPED",
                    message=msg,
                )
            self._logger.error("Alpaca bracket order submission failed: %s", msg)
            raise

    def flatten_symbol_eod(
        self,
        symbol: str | None = None,
        *,
        session_date: date | None = None,
        force_unowned: bool = False,
        cancel_poll_attempts: int = 6,
        cancel_poll_sleep_sec: float = 0.4,
    ) -> dict[str, object]:
        """
        Safely flatten bot-owned intraday exposure for `symbol` before the 16:00 ET close:
          1. Verify ownership via `spydt-*` orders or `SQLiteStateLedger` (protects external swing positions
             unless `force_unowned=True` is explicitly passed via manual CLI).
          2. Cancel open child bracket orders for `symbol` first (frees `held_for_orders` shares).
          3. Submit `close_position(symbol)` once shares are released.
        """
        target = (symbol or self._config.symbol).upper()
        open_orders = self.get_open_symbol_orders(target)
        pos_qty = self.get_open_symbol_position_qty(target)

        has_spydt_order = any(
            str(getattr(o, "client_order_id", "") or "").startswith(BOT_ORDER_PREFIX)
            for o in open_orders
        )
        ledger_owned = (
            self._ledger.has_active_bot_symbol_session_orders(target, session_date)
            if (self._ledger is not None and session_date is not None)
            else (self._ledger is None)
        )

        if not force_unowned and (pos_qty != 0.0 or open_orders) and not (has_spydt_order or ledger_owned):
            self._logger.warning(
                "EOD flatten skipped on %s: open exposure not originated by %s* ledger today (protecting external position).",
                target,
                BOT_ORDER_PREFIX,
            )
            return {
                "symbol": target,
                "cancelled_order_ids": [],
                "closed_position": False,
                "protected_external": True,
                "prior_qty": pos_qty,
            }

        cancelled_ids: list[str] = []
        for order in open_orders:
            oid = getattr(order, "id", None)
            if oid is not None:
                try:
                    self._client.cancel_order_by_id(oid)
                    cancelled_ids.append(str(oid))
                except APIError as exc:
                    self._logger.warning("Order cancel warning for %s: %s", oid, exc)

        if cancelled_ids:
            for _ in range(cancel_poll_attempts):
                if not self.get_open_symbol_orders(target):
                    break
                time_mod.sleep(cancel_poll_sleep_sec)

        pos_qty = self.get_open_symbol_position_qty(target)
        closed_position = False
        if pos_qty != 0.0:
            self._client.close_position(target)
            closed_position = True
            self._logger.info(
                "EOD FLATTEN executed for %s (closed qty=%g, cancelled_orders=%d)",
                target,
                pos_qty,
                len(cancelled_ids),
            )
        elif cancelled_ids:
            self._logger.info(
                "EOD CLEANUP cancelled %d working orders for %s (position already flat)",
                len(cancelled_ids),
                target,
            )

        return {
            "symbol": target,
            "cancelled_order_ids": cancelled_ids,
            "closed_position": closed_position,
            "protected_external": False,
            "prior_qty": pos_qty,
        }
