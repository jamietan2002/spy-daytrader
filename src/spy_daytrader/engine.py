"""Boundary-snapped 1m execution engine with SQLite crash-safe bar dedup & stale-entry cleanup."""

from __future__ import annotations

import signal
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from spy_daytrader.broker import AlpacaPaperBroker, OrderSubmissionReceipt
from spy_daytrader.config import BotConfig
from spy_daytrader.data_feed import NY_TZ, MarketDataFeed
from spy_daytrader.indicators import compute_indicators
from spy_daytrader.logging_utils import get_logger
from spy_daytrader.risk import BracketOrderPlan, calculate_bracket_order_plan
from spy_daytrader.state_store import SQLiteStateLedger
from spy_daytrader.strategy import SignalAction, SignalDecision, evaluate_signal


@dataclass(frozen=True)
class CycleOutcome:
    """Structured telemetry returned after each 1-minute bot iteration."""

    timestamp_et: datetime
    market_open: bool
    status: str
    signal: SignalDecision | None = None
    order_plan: BracketOrderPlan | None = None
    receipt: OrderSubmissionReceipt | None = None


class IntradayTradingBot:
    """
    Production v2 intraday trading engine:
      - Boundary-snapped 1-minute loop waking at `:00:01.5s` for sub-2s closed-bar execution
      - Crash-safe SQLite state ledger (`data/bot_state.db`) for bar dedup & bot-scoped P&L
      - Auto-cancels stale unfilled Marketable Limit parent entries after 55s
      - Bot-isolated `spydt-*` 15:55 ET EOD flatten protecting external account holdings
    """

    def __init__(
        self,
        config: BotConfig,
        *,
        data_feed: MarketDataFeed | None = None,
        broker: AlpacaPaperBroker | None = None,
        ledger: SQLiteStateLedger | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.logger = get_logger("spy_daytrader.engine")
        self.ledger = ledger or SQLiteStateLedger(config.state_db_path)
        self.data_feed = data_feed or MarketDataFeed(config)
        self.broker = broker or AlpacaPaperBroker(config, ledger=self.ledger)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._running = True

    def request_stop(self, *_args: object) -> None:
        self.logger.info("Graceful shutdown signal received; finishing current cycle...")
        self._running = False

    def run_once(self, *, dry_run: bool = False) -> CycleOutcome:
        """Execute a single 1-minute supervision & signal evaluation cycle."""
        now_utc = self._now_fn()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_et = now_utc.astimezone(NY_TZ)
        session_date = now_et.date()

        clock = self.broker.get_market_clock()
        account = self.broker.get_account_snapshot()

        if not clock.is_open:
            self.logger.info(
                "Market CLOSED (%s ET) | next_open=%s | equity=$%.2f | mode=%s",
                now_et.strftime("%Y-%m-%d %H:%M:%S"),
                clock.next_open.astimezone(NY_TZ).strftime("%Y-%m-%d %H:%M ET"),
                account.equity,
                self.config.strategy_mode.value,
            )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=False,
                status="MARKET_CLOSED",
            )

        # 1. EOD Intraday Flatten Guard (>= 15:55 ET) — isolated to spydt-* owned exposure
        if now_et.time() >= self.config.eod_flatten_et:
            if not dry_run:
                flatten_res = self.broker.flatten_symbol_eod(
                    self.config.symbol,
                    session_date=session_date,
                )
                if flatten_res["closed_position"] or flatten_res["cancelled_order_ids"]:
                    return CycleOutcome(
                        timestamp_et=now_et,
                        market_open=True,
                        status="EOD_FLATTENED",
                    )
                if flatten_res.get("protected_external"):
                    return CycleOutcome(
                        timestamp_et=now_et,
                        market_open=True,
                        status="EOD_EXTERNAL_POSITION_PROTECTED",
                    )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status="EOD_NO_NEW_ENTRIES",
            )

        # 2. Reconcile & auto-cancel stale unfilled parent Marketable Limit entries (> 55s old)
        if not dry_run:
            self.broker.cancel_stale_unfilled_bot_entries(
                self.config.symbol,
                now_utc=now_utc,
            )

        # 3. Fetch latest closed 5-minute RTH bars & compute causal indicators
        bars_df = self.data_feed.fetch_closed_bars(now_utc=now_utc)
        enriched_df = compute_indicators(bars_df, self.config)
        decision = evaluate_signal(enriched_df, self.config)

        has_exposure, exposure_desc = self.broker.has_open_exposure(self.config.symbol)

        self.logger.info(
            "Bar %s | %s close=%.2f EMA50=%.2f EMA200=%.2f VWAP=%.2f StochRSI(K=%.1f, D=%.1f) ATR=%.2f | "
            "exposure=%s | daily_pnl=%+.2f%% | signal=%s (%s)",
            decision.bar_close_et.strftime("%H:%M") if decision.bar_close_et else "N/A",
            self.config.symbol,
            decision.close,
            decision.ema_50,
            decision.ema_200,
            decision.session_vwap,
            decision.stochrsi_k,
            decision.stochrsi_d,
            decision.atr_14,
            exposure_desc,
            account.daily_pnl_pct * 100.0,
            decision.action.value,
            decision.reason,
        )

        if has_exposure:
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status=f"HOLDING_OR_WORKING:{exposure_desc}",
                signal=decision,
            )

        if decision.action == SignalAction.NONE or decision.bar_close_et is None:
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status=f"NO_TRADE:{decision.reason}",
                signal=decision,
            )

        # 4. Crash-safe SQLite deduplication: never evaluate/submit twice on the same closed 5m bar
        mode_str = self.config.strategy_mode.value
        if self.ledger.has_processed_bar(self.config.symbol, mode_str, decision.bar_close_et):
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status="BAR_ALREADY_PROCESSED",
                signal=decision,
            )

        order_side = "BUY" if decision.action == SignalAction.BUY else "SELL"
        exec_ref_price = self.data_feed.fetch_executable_quote_price(
            order_side,
            fallback_close=decision.close,
        )
        bot_session_pnl = self.ledger.get_session_realized_pnl(session_date)

        plan = calculate_bracket_order_plan(
            account=account,
            side=order_side,
            reference_price=exec_ref_price,
            atr_14=decision.atr_14,
            bar_close_et=decision.bar_close_et,
            config=self.config,
            bot_session_realized_pnl_usd=bot_session_pnl,
        )

        if not plan.approved:
            self.logger.warning(
                "Risk Manager VETOED %s signal on %s: %s",
                order_side,
                self.config.symbol,
                plan.veto_reason,
            )
            self.ledger.mark_bar_processed(
                self.config.symbol,
                mode_str,
                decision.bar_close_et,
                decision.action.value,
                f"RISK_VETO:{plan.veto_reason}",
            )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status=f"RISK_VETO:{plan.veto_reason}",
                signal=decision,
                order_plan=plan,
            )

        if dry_run:
            self.logger.info(
                "[DRY-RUN] Would submit %s BRACKET %s %d %s @ ref=%.2f lim=%s (SL=%.2f, TP=%.2f, risk=$%.2f)",
                plan.order_type,
                plan.side,
                plan.qty,
                plan.symbol,
                plan.reference_price,
                f"{plan.limit_price:.2f}" if plan.limit_price is not None else "MKT",
                plan.stop_loss_price,
                plan.take_profit_price,
                plan.actual_risk_usd,
            )
            self.ledger.mark_bar_processed(
                self.config.symbol,
                mode_str,
                decision.bar_close_et,
                decision.action.value,
                "DRY_RUN_APPROVED",
            )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status="DRY_RUN_APPROVED",
                signal=decision,
                order_plan=plan,
            )

        receipt = self.broker.submit_bracket_order(plan, session_date=session_date)
        self.ledger.mark_bar_processed(
            self.config.symbol,
            mode_str,
            decision.bar_close_et,
            decision.action.value,
            f"SUBMITTED:{receipt.status}",
        )
        return CycleOutcome(
            timestamp_et=now_et,
            market_open=True,
            status="ORDER_SUBMITTED" if receipt.submitted else f"ORDER_NOT_SUBMITTED:{receipt.status}",
            signal=decision,
            order_plan=plan,
            receipt=receipt,
        )

    def run_forever(
        self,
        *,
        dry_run: bool = False,
        max_cycles: int | None = None,
    ) -> None:
        """Run the boundary-snapped (`:00:01.5s`) 1-minute intraday trading loop."""
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        self.logger.info(
            "Starting IntradayTradingBot v2 | symbol=%s | mode=%s | atr_profile=%s (%.2fx/%.2fx) | "
            "poll=%ds (+%.1fs snap) | db=%s | dry_run=%s",
            self.config.symbol,
            self.config.strategy_mode.value,
            self.config.atr_profile.value,
            self.config.stop_loss_atr_mult,
            self.config.take_profit_atr_mult,
            self.config.poll_interval_seconds,
            self.config.boundary_wakeup_offset_sec,
            self.config.state_db_path,
            dry_run,
        )

        cycles = 0
        while self._running:
            cycle_started = time_mod.monotonic()
            try:
                self.run_once(dry_run=dry_run)
            except Exception as exc:
                self.logger.exception("Unhandled error in 1-minute trading cycle (recovering): %s", exc)

            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            if not self._running:
                break

            now = time_mod.time()
            interval = float(self.config.poll_interval_seconds)
            elapsed = time_mod.monotonic() - cycle_started
            if interval == 60.0:
                sleep_sec = max(
                    0.5,
                    (60.0 - (now % 60.0)) + self.config.boundary_wakeup_offset_sec,
                )
            else:
                sleep_sec = max(0.5, interval - elapsed)

            slept = 0.0
            while self._running and slept < sleep_sec:
                step = min(0.5, sleep_sec - slept)
                time_mod.sleep(step)
                slept += step

        self.logger.info("IntradayTradingBot stopped cleanly after %d cycle(s).", cycles)
