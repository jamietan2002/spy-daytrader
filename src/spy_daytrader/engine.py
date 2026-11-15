"""Boundary-snapped 1m execution engine with SQLite heartbeat, smart off-hours sleep, & bar dedup."""

from __future__ import annotations

import signal
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, time, timezone
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
    """Structured telemetry returned after each bot iteration."""

    timestamp_et: datetime
    market_open: bool
    status: str
    next_open_utc: datetime | None = None
    signal: SignalDecision | None = None
    order_plan: BracketOrderPlan | None = None
    receipt: OrderSubmissionReceipt | None = None


class IntradayTradingBot:
    """
    Production v2 intraday trading engine:
      - Boundary-snapped 1-minute loop waking at `:00:01.5s` during NYSE market hours
      - Smart low-power sleep outside market hours (wakes automatically before market open)
      - Crash-safe SQLite state ledger (`data/bot_state.db`) for heartbeats, bar dedup & P&L
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

    def _record_hb(
        self,
        *,
        market_open: bool,
        status: str,
        next_open_utc: datetime | None,
        now_utc: datetime,
    ) -> None:
        self.ledger.record_heartbeat(
            symbol=self.config.symbol,
            mode=self.config.strategy_mode.value,
            market_open=market_open,
            status=status,
            next_open_utc=next_open_utc,
            updated_at_utc=now_utc,
        )

    def run_once(self, *, dry_run: bool = False) -> CycleOutcome:
        """Execute a single 1-minute supervision & signal evaluation cycle."""
        now_utc = self._now_fn()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_et = now_utc.astimezone(NY_TZ)
        session_date = now_et.date()

        clock = self.broker.get_market_clock()
        account = self.broker.get_account_snapshot()
        next_open_utc = clock.next_open.astimezone(timezone.utc)

        if not clock.is_open:
            self.logger.info(
                "Market CLOSED (%s ET) | next_open=%s | equity=$%.2f | mode=%s",
                now_et.strftime("%Y-%m-%d %H:%M:%S"),
                clock.next_open.astimezone(NY_TZ).strftime("%Y-%m-%d %H:%M ET"),
                account.equity,
                self.config.strategy_mode.value,
            )
            self._record_hb(
                market_open=False,
                status="MARKET_CLOSED",
                next_open_utc=next_open_utc,
                now_utc=now_utc,
            )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=False,
                status="MARKET_CLOSED",
                next_open_utc=next_open_utc,
            )

        # 1. EOD Intraday Flatten Guard (>= 15:55 ET or <= 5m before early-close half-day)
        next_close_utc = (
            clock.next_close.astimezone(timezone.utc) if clock.next_close is not None else None
        )
        seconds_to_close = (
            (next_close_utc - now_utc).total_seconds()
            if next_close_utc is not None
            else 999_999.0
        )
        if now_et.time() >= self.config.eod_flatten_et or (0.0 <= seconds_to_close <= 300.0):
            status_code = "EOD_NO_NEW_ENTRIES"
            if not dry_run:
                flatten_res = self.broker.flatten_symbol_eod(
                    self.config.symbol,
                    session_date=session_date,
                )
                if flatten_res["closed_position"] or flatten_res["cancelled_order_ids"]:
                    status_code = "EOD_FLATTENED"
                elif flatten_res.get("protected_external"):
                    status_code = "EOD_EXTERNAL_POSITION_PROTECTED"
            self._record_hb(
                market_open=True,
                status=status_code,
                next_open_utc=next_open_utc,
                now_utc=now_utc,
            )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status=status_code,
                next_open_utc=next_open_utc,
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
            status_str = f"HOLDING_OR_WORKING:{exposure_desc}"
            self._record_hb(
                market_open=True,
                status=status_str,
                next_open_utc=next_open_utc,
                now_utc=now_utc,
            )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status=status_str,
                next_open_utc=next_open_utc,
                signal=decision,
            )

        if decision.action == SignalAction.NONE or decision.bar_close_et is None:
            status_str = f"NO_TRADE:{decision.reason}"
            self._record_hb(
                market_open=True,
                status=status_str,
                next_open_utc=next_open_utc,
                now_utc=now_utc,
            )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status=status_str,
                next_open_utc=next_open_utc,
                signal=decision,
            )

        # 4. Crash-safe SQLite deduplication
        mode_str = self.config.strategy_mode.value
        if self.ledger.has_processed_bar(self.config.symbol, mode_str, decision.bar_close_et):
            self._record_hb(
                market_open=True,
                status="BAR_ALREADY_PROCESSED",
                next_open_utc=next_open_utc,
                now_utc=now_utc,
            )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status="BAR_ALREADY_PROCESSED",
                next_open_utc=next_open_utc,
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
            status_str = f"RISK_VETO:{plan.veto_reason}"
            self.ledger.mark_bar_processed(
                self.config.symbol,
                mode_str,
                decision.bar_close_et,
                decision.action.value,
                status_str,
            )
            self._record_hb(
                market_open=True,
                status=status_str,
                next_open_utc=next_open_utc,
                now_utc=now_utc,
            )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status=status_str,
                next_open_utc=next_open_utc,
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
            self._record_hb(
                market_open=True,
                status="DRY_RUN_APPROVED",
                next_open_utc=next_open_utc,
                now_utc=now_utc,
            )
            return CycleOutcome(
                timestamp_et=now_et,
                market_open=True,
                status="DRY_RUN_APPROVED",
                next_open_utc=next_open_utc,
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
        final_status = (
            "ORDER_SUBMITTED" if receipt.submitted else f"ORDER_NOT_SUBMITTED:{receipt.status}"
        )
        self._record_hb(
            market_open=True,
            status=final_status,
            next_open_utc=next_open_utc,
            now_utc=now_utc,
        )
        return CycleOutcome(
            timestamp_et=now_et,
            market_open=True,
            status=final_status,
            next_open_utc=next_open_utc,
            signal=decision,
            order_plan=plan,
            receipt=receipt,
        )

    def run_forever(
        self,
        *,
        dry_run: bool = False,
        max_cycles: int | None = None,
        register_signals: bool = True,
    ) -> None:
        """
        Run the autonomous 24/7 loop:
          - During market hours: boundary-snapped (`:00:01.5s`) 1-minute precision polling.
          - Outside market hours: low-overhead 5-minute heartbeat checks, switching back to
            1-minute precision automatically 2 minutes before NYSE market open.
        """
        if register_signals:
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
            outcome: CycleOutcome | None = None
            try:
                outcome = self.run_once(dry_run=dry_run)
            except Exception as exc:
                self.logger.exception("Unhandled error in trading cycle (recovering): %s", exc)

            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            if not self._running:
                break

            now = time_mod.time()
            interval = float(self.config.poll_interval_seconds)
            elapsed = time_mod.monotonic() - cycle_started

            # Smart off-hours sleep: check every <=300s until 2 minutes before next NYSE open
            if outcome is not None and not outcome.market_open and outcome.next_open_utc is not None:
                until_open = (
                    outcome.next_open_utc - datetime.now(timezone.utc)
                ).total_seconds()
                if until_open > 150.0:
                    sleep_sec = min(300.0, max(30.0, until_open - 90.0))
                else:
                    sleep_sec = max(1.0, (60.0 - (now % 60.0)) + self.config.boundary_wakeup_offset_sec)
            elif interval == 60.0:
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

    def run_market_session(
        self,
        *,
        until_et: time | None = None,
        dry_run: bool = False,
        max_cycles: int | None = None,
        register_signals: bool = True,
    ) -> int:
        """
        Bounded NYSE market-session runner tailored for Free-Forever GitHub Actions crons:
          1. Exits immediately (0s wasted) on weekends, NYSE holidays, or if today's session already closed.
          2. If launched pre-open on an active trading day, waits cleanly until NYSE market open.
          3. Polls boundary-snapped every 1 minute (`:00:01.5s`) until `until_et` handoff or market close.
        """
        if register_signals:
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)

        now_utc = self._now_fn()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_et = now_utc.astimezone(NY_TZ)

        if until_et is not None and now_et.time() >= until_et:
            self.logger.info(
                "Current time %s ET is already >= session cutoff %s ET; exiting cleanly.",
                now_et.strftime("%H:%M:%S"),
                until_et.strftime("%H:%M"),
            )
            return 0

        clock = self.broker.get_market_clock()
        next_open_et = clock.next_open.astimezone(NY_TZ)

        if not clock.is_open and next_open_et.date() != now_et.date():
            self.logger.info(
                "No remaining NYSE session today (%s ET; next open %s ET). Recording heartbeat and exiting.",
                now_et.strftime("%Y-%m-%d %H:%M"),
                next_open_et.strftime("%Y-%m-%d %H:%M"),
            )
            self.run_once(dry_run=dry_run)
            return 0

        # Pre-market standby on an active trading day
        if not clock.is_open and next_open_et.date() == now_et.date():
            wait_sec = max(0.0, (clock.next_open.astimezone(timezone.utc) - now_utc).total_seconds() - 2.0)
            self.logger.info(
                "Active trading day pre-open (%s ET); waiting %.0fs for NYSE open at %s ET...",
                now_et.strftime("%H:%M:%S"),
                wait_sec,
                next_open_et.strftime("%H:%M"),
            )
            self._record_hb(
                market_open=False,
                status="PRE_MARKET_STANDBY",
                next_open_utc=clock.next_open.astimezone(timezone.utc),
                now_utc=now_utc,
            )
            slept = 0.0
            while self._running and slept < wait_sec:
                cur_et = self._now_fn().astimezone(NY_TZ)
                if until_et is not None and cur_et.time() >= until_et:
                    return 0
                step = min(0.5, wait_sec - slept)
                time_mod.sleep(step)
                slept += step

        saw_market_open = bool(clock.is_open)
        cycles = 0
        while self._running:
            cycle_started = time_mod.monotonic()
            outcome: CycleOutcome | None = None
            try:
                outcome = self.run_once(dry_run=dry_run)
                if outcome.market_open:
                    saw_market_open = True
            except Exception as exc:
                self.logger.exception("Unhandled error in session cycle (recovering): %s", exc)

            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            if not self._running:
                break

            cur_et = self._now_fn().astimezone(NY_TZ)
            if until_et is not None and cur_et.time() >= until_et:
                self.logger.info(
                    "Reached scheduled session handoff (%s ET); exiting cleanly.",
                    until_et.strftime("%H:%M"),
                )
                break
            if saw_market_open and outcome is not None and not outcome.market_open:
                self.logger.info("NYSE market session closed for today; exiting cleanly.")
                break

            now_epoch = time_mod.time()
            interval = float(self.config.poll_interval_seconds)
            elapsed = time_mod.monotonic() - cycle_started
            if interval == 60.0:
                sleep_sec = max(0.5, (60.0 - (now_epoch % 60.0)) + self.config.boundary_wakeup_offset_sec)
            else:
                sleep_sec = max(0.5, interval - elapsed)

            slept = 0.0
            while self._running and slept < sleep_sec:
                step = min(0.5, sleep_sec - slept)
                time_mod.sleep(step)
                slept += step

        self.logger.info("Session runner finished cleanly after %d cycle(s).", cycles)
        return 0
