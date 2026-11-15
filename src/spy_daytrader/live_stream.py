"""Event-driven Alpaca `TradingStream` WebSocket listener & thread-safe RAM snapshot hub."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable

from alpaca.trading.stream import TradingStream

from spy_daytrader.config import (
    PAPER_TRADING_BASE_URL,
    BotConfig,
    LiveTradingProhibitedError,
)
from spy_daytrader.dashboard_data import (
    DashboardSnapshot,
    _estimate_nyse_clock,
    build_header_status,
    fetch_dashboard_snapshot,
)
from spy_daytrader.logging_utils import get_logger
from spy_daytrader.state_store import SQLiteStateLedger

RELEVANT_TRADE_EVENTS: frozenset[str] = frozenset(
    {
        "new",
        "accepted",
        "fill",
        "partial_fill",
        "canceled",
        "cancelled",
        "expired",
        "done_for_day",
        "replaced",
        "rejected",
        "held",
        "stopped",
        "OrderEvent.NEW",
        "OrderEvent.FILL",
        "OrderEvent.PARTIAL_FILL",
        "OrderEvent.CANCELED",
        "OrderEvent.EXPIRED",
        "OrderEvent.REPLACED",
        "OrderEvent.REJECTED",
    }
)


def _normalize_event_name(raw_event: Any) -> str:
    text = str(raw_event or "").strip()
    if "." in text:
        text = text.split(".")[-1]
    return text.lower()


def _db_mtime_signature(db_path_str: str) -> tuple[int, int]:
    if not db_path_str or db_path_str == ":memory:":
        return (0, 0)
    db_path = Path(db_path_str)
    wal_path = Path(f"{db_path_str}-wal")
    main_ns = db_path.stat().st_mtime_ns if db_path.exists() else 0
    wal_ns = wal_path.stat().st_mtime_ns if wal_path.exists() else 0
    return (main_ns, wal_ns)


class BrokerTradeStreamHub:
    """
    Process-wide singleton hub shared across all Streamlit UI sessions via `@st.cache_resource`:
      1. Warms an in-memory `DashboardSnapshot` once at startup so UI tabs & journal clicks
         render in `<0.5 ms` with zero blocking HTTP calls.
      2. Subscribes a single background daemon thread to Alpaca's `TradingStream` WebSocket
         (`wss://paper-api.alpaca.markets/stream`) and refreshes the cached snapshot
         automatically whenever an actual broker order/fill event arrives, incrementing `version`.
      3. Performs zero periodic Alpaca REST polling while flat with an active WebSocket;
         only refreshes mark-to-market in the background once per 60s when an open trade is held.
      4. Falls back seamlessly to `mtime`-cached `data/bot_state.db` reads if API keys are unset.
    """

    def __init__(
        self,
        config: BotConfig,
        *,
        fetch_fn: Callable[[BotConfig], DashboardSnapshot] = fetch_dashboard_snapshot,
        stream_factory: Callable[[BotConfig], Any] | None = None,
        enable_websocket: bool = True,
        coalesce_delay_sec: float = 0.15,
        open_position_refresh_sec: float = 60.0,
        offline_clock_refresh_sec: float = 60.0,
        monotonic_fn: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._assert_paper_safety(config)
        self._config = config
        self._fetch_fn = fetch_fn
        self._stream_factory = stream_factory
        self._enable_websocket = enable_websocket
        self._coalesce_delay_sec = max(0.0, float(coalesce_delay_sec))
        self._open_position_refresh_sec = max(5.0, float(open_position_refresh_sec))
        self._offline_clock_refresh_sec = max(10.0, float(offline_clock_refresh_sec))
        self._monotonic_fn = monotonic_fn
        self._now_fn = now_fn
        self._logger = get_logger("spy_daytrader.live_stream")

        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()

        self._snapshot: DashboardSnapshot | None = None
        self._version: int = 0
        self._refresh_requested: bool = False
        self._bg_refresh_inflight: bool = False
        self._last_refresh_mono: float = 0.0
        self._last_db_mtime: tuple[int, int] = (0, 0)

        self._stream: Any | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_connected_override: bool = False
        self._observed_ws_live: bool = False
        self._last_event_name: str | None = None
        self._last_event_utc: datetime | None = None

    @staticmethod
    def _assert_paper_safety(config: BotConfig) -> None:
        if not config.alpaca_paper:
            raise LiveTradingProhibitedError(
                "LIVE TRADING BLOCKED: BrokerTradeStreamHub strictly requires alpaca_paper=True."
            )
        norm_url = config.alpaca_base_url.strip().rstrip("/")
        if norm_url.endswith("/v2"):
            norm_url = norm_url[:-3]
        if norm_url != PAPER_TRADING_BASE_URL:
            raise LiveTradingProhibitedError(
                f"LIVE TRADING BLOCKED: alpaca_base_url={config.alpaca_base_url!r} is not Paper."
            )

    @property
    def has_live_credentials(self) -> bool:
        return bool(self._config.alpaca_api_key.strip() and self._config.alpaca_secret_key.strip())

    @property
    def stream_connected(self) -> bool:
        stream_obj = self._stream
        is_running = bool(stream_obj is not None and getattr(stream_obj, "_running", False))
        return bool(self._ws_connected_override or is_running)

    @property
    def version(self) -> int:
        # Detect initial WebSocket handshake completion so open tabs pick up live status cleanly
        is_conn = self.stream_connected
        with self._lock:
            if is_conn and not self._observed_ws_live and self._snapshot is not None:
                self._observed_ws_live = True
                self._snapshot = self._apply_stream_header(self._snapshot, stream_connected=True)
                self._version += 1
            elif not is_conn and self._observed_ws_live and self._snapshot is not None:
                self._observed_ws_live = False
                self._snapshot = self._apply_stream_header(self._snapshot, stream_connected=False)
                self._version += 1
            return self._version

    @property
    def last_event_name(self) -> str | None:
        with self._lock:
            return self._last_event_name

    @property
    def last_event_utc(self) -> datetime | None:
        with self._lock:
            return self._last_event_utc

    def _apply_stream_header(
        self,
        snap: DashboardSnapshot,
        *,
        stream_connected: bool,
    ) -> DashboardSnapshot:
        hb = None
        try:
            hb = SQLiteStateLedger(self._config.state_db_path).get_latest_heartbeat()
        except Exception:
            hb = None
        hdr = build_header_status(
            market_open=snap.market_open,
            next_open_utc=snap.next_open_utc,
            heartbeat=hb,
            reference_utc=self._now_fn(),
            stream_connected=stream_connected,
        )
        if hdr == snap.header_status_line:
            return snap
        return replace(snap, header_status_line=hdr)

    def start(self) -> "BrokerTradeStreamHub":
        """Warm initial RAM snapshot and start background Alpaca `TradingStream` daemon if configured."""
        self.trigger_refresh(reason="startup")
        if self.has_live_credentials and self._enable_websocket:
            with self._lock:
                already_alive = self._ws_thread is not None and self._ws_thread.is_alive()
                if not already_alive:
                    self._stop_event.clear()
                    self._ws_thread = threading.Thread(
                        target=self._run_stream_supervisor,
                        name="alpaca-trade-stream",
                        daemon=True,
                    )
                    self._ws_thread.start()
        return self

    def stop(self) -> None:
        """Stop the background WebSocket stream cleanly."""
        self._stop_event.set()
        stream_obj = self._stream
        if stream_obj is not None:
            try:
                stream_obj.stop()
            except Exception:
                pass

    def _build_trading_stream(self) -> Any:
        self._assert_paper_safety(self._config)
        if self._stream_factory is not None:
            return self._stream_factory(self._config)
        return TradingStream(
            api_key=self._config.alpaca_api_key,
            secret_key=self._config.alpaca_secret_key,
            paper=True,
        )

    async def handle_trade_update(self, data: Any) -> None:
        """
        Async callback invoked by `TradingStream` whenever Alpaca pushes an order lifecycle event
        (`new`, `fill`, `partial_fill`, `canceled`, `expired`, `replaced`, `rejected`).
        """
        raw_evt = getattr(data, "event", None)
        if raw_evt is None and isinstance(data, dict):
            raw_evt = data.get("event")
        evt_name = _normalize_event_name(raw_evt)

        order_obj = getattr(data, "order", None)
        if order_obj is None and isinstance(data, dict):
            order_obj = data.get("order")
        sym = str(
            getattr(order_obj, "symbol", None)
            or (order_obj.get("symbol") if isinstance(order_obj, dict) else "")
            or self._config.symbol
        ).upper()

        now_utc = self._now_fn()
        with self._lock:
            self._last_event_name = evt_name or "unknown"
            self._last_event_utc = now_utc

        if evt_name and evt_name not in RELEVANT_TRADE_EVENTS:
            return

        self._logger.info(
            "Received live Alpaca trade_updates event=%s symbol=%s; refreshing RAM snapshot.",
            evt_name or "update",
            sym,
        )
        if self._coalesce_delay_sec > 0.0:
            await asyncio.sleep(self._coalesce_delay_sec)
        await asyncio.to_thread(self.trigger_refresh, f"ws:{evt_name or 'update'}")

    def _run_stream_supervisor(self) -> None:
        backoff_sec = 2.0
        while not self._stop_event.is_set():
            try:
                stream = self._build_trading_stream()
                stream.subscribe_trade_updates(self.handle_trade_update)
                self._stream = stream
                self._logger.info("Connecting Alpaca Paper TradingStream WebSocket listener...")
                stream.run()
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                self._logger.warning(
                    "Alpaca TradingStream listener exited (%s); reconnecting in %.1fs...",
                    exc,
                    backoff_sec,
                )
            finally:
                self._stream = None

            if self._stop_event.wait(timeout=backoff_sec):
                break
            backoff_sec = min(30.0, backoff_sec * 2.0)

    def trigger_refresh(self, reason: str = "manual") -> DashboardSnapshot:
        """
        Synchronously fetch a fresh `DashboardSnapshot` and swap it atomically into RAM.
        Coalesces concurrent callers so rapid multi-leg fills never stampede Alpaca's REST API
        while guaranteeing the final post-fill state is always captured.
        """
        with self._lock:
            self._refresh_requested = True

        if not self._refresh_lock.acquire(blocking=False):
            with self._lock:
                if self._snapshot is not None:
                    return self._snapshot
            self._refresh_lock.acquire()

        try:
            while True:
                with self._lock:
                    self._refresh_requested = False

                snap = self._fetch_fn(self._config)
                is_conn = self.stream_connected
                if is_conn:
                    snap = self._apply_stream_header(snap, stream_connected=True)
                now_mono = self._monotonic_fn()
                mtime_sig = _db_mtime_signature(self._config.state_db_path)

                with self._lock:
                    self._snapshot = snap
                    self._observed_ws_live = is_conn
                    self._last_refresh_mono = now_mono
                    self._last_db_mtime = mtime_sig
                    self._version += 1
                    self._logger.debug(
                        "Dashboard RAM snapshot refreshed (reason=%s, version=%d)",
                        reason,
                        self._version,
                    )
                    if not self._refresh_requested:
                        return snap
        finally:
            with self._lock:
                self._bg_refresh_inflight = False
            self._refresh_lock.release()

    def _spawn_background_refresh(self, reason: str) -> None:
        with self._lock:
            if self._bg_refresh_inflight:
                return
            self._bg_refresh_inflight = True

        def _worker() -> None:
            try:
                self.trigger_refresh(reason=reason)
            except Exception as exc:
                with self._lock:
                    self._bg_refresh_inflight = False
                self._logger.warning("Background snapshot refresh (%s) failed: %s", reason, exc)

        threading.Thread(target=_worker, name=f"snap-refresh-{reason}", daemon=True).start()

    def check_background_refresh(self, config: BotConfig | None = None) -> None:
        """
        Non-blocking maintenance check:
          - If flat + WebSocket connected + no bell crossing: 0 network/disk calls.
          - If holding an active open position during market hours: refreshes unrealized P&L
            once per 60s on a background thread so UI clicks never block.
          - If NYSE open/close bell just crossed: refreshes market status in the background.
          - In zero-credential SQLite mode: refreshes only if `bot_state.db` `mtime` changed
            (or once per 60s to advance clock text).
        """
        if config is not None and config != self._config:
            self._assert_paper_safety(config)
            self._config = config

        with self._lock:
            snap = self._snapshot
            last_mono = self._last_refresh_mono
            last_mtime = self._last_db_mtime

        if snap is None:
            return

        age_sec = max(0.0, self._monotonic_fn() - last_mono)
        now_utc = self._now_fn()

        if not self.has_live_credentials:
            cur_mtime = _db_mtime_signature(self._config.state_db_path)
            if cur_mtime != last_mtime or age_sec >= self._offline_clock_refresh_sec:
                self._spawn_background_refresh("sqlite_mtime_or_clock")
            return

        # 1. Check NYSE opening / closing bell boundary crossing
        est_open, _ = _estimate_nyse_clock(now_utc, None)
        if est_open != snap.market_open and age_sec >= 15.0:
            self._spawn_background_refresh("bell_transition")
            return

        # 2. Mark-to-market unrealized P&L once a minute ONLY while holding an open trade
        if snap.market_open and snap.open_trade is not None and age_sec >= self._open_position_refresh_sec:
            self._spawn_background_refresh("open_position_mark")
            return

        # 3. Safety-net fallback ONLY if WebSocket is disconnected during market hours
        if snap.market_open and not self.stream_connected and age_sec >= self._open_position_refresh_sec:
            self._spawn_background_refresh("ws_fallback_poll")

    def get_snapshot(
        self,
        config: BotConfig | None = None,
    ) -> tuple[DashboardSnapshot, int, bool]:
        """
        Return `(snapshot, version, stream_connected)` from RAM in `<0.5 ms` without blocking
        the Streamlit render thread on network I/O (after initial warm-up).
        """
        if config is not None and config != self._config:
            self._assert_paper_safety(config)
            self._config = config

        # Touch `self.version` first so WebSocket connection state changes apply cleanly
        _ = self.version

        with self._lock:
            snap = self._snapshot
            ver = self._version

        if snap is None:
            snap = self.trigger_refresh(reason="lazy_init")
            with self._lock:
                ver = self._version
            return snap, ver, self.stream_connected

        self.check_background_refresh()
        with self._lock:
            return self._snapshot or snap, self._version, self.stream_connected
