"""Crash-safe SQLite state ledger for bar deduplication, bot-owned orders, heartbeat, and daily P&L."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

BOT_ORDER_PREFIX = "spydt-"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS processed_bars (
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    bar_close_iso TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (symbol, mode, bar_close_iso)
);

CREATE TABLE IF NOT EXISTS bot_orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    qty INTEGER NOT NULL,
    reference_price REAL NOT NULL,
    limit_price REAL,
    stop_loss_price REAL NOT NULL,
    take_profit_price REAL NOT NULL,
    status TEXT NOT NULL,
    session_date TEXT NOT NULL,
    realized_pnl_usd REAL NOT NULL DEFAULT 0.0,
    submitted_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_heartbeat (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    market_open INTEGER NOT NULL,
    status TEXT NOT NULL,
    next_open_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dashboard_cache (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    payload_json TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class TrackedBotOrder:
    """Persisted record of an order originated by spy-daytrader (`spydt-*`)."""

    client_order_id: str
    broker_order_id: str
    symbol: str
    mode: str
    side: str
    order_type: str
    qty: int
    reference_price: float
    limit_price: float | None
    stop_loss_price: float
    take_profit_price: float
    status: str
    session_date: str
    realized_pnl_usd: float
    submitted_at_utc: datetime


@dataclass(frozen=True)
class BotHeartbeat:
    """Latest background worker heartbeat read by the monitoring UI."""

    symbol: str
    mode: str
    market_open: bool
    status: str
    next_open_utc: datetime | None
    updated_at_utc: datetime


class SQLiteStateLedger:
    """Lightweight WAL-mode SQLite persistence guaranteeing crash-safe idempotency & isolation."""

    def __init__(self, db_path: str | Path = "data/bot_state.db") -> None:
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        if str(self.db_path) == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:")
            self._memory_conn.row_factory = sqlite3.Row
            self._memory_conn.executescript(_SCHEMA_SQL)
        else:
            try:
                with self._connect() as conn:
                    conn.executescript(_SCHEMA_SQL)
            except sqlite3.OperationalError:
                pass

    def _connect(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError:
            pass
        return conn

    def record_heartbeat(
        self,
        *,
        symbol: str,
        mode: str,
        market_open: bool,
        status: str,
        next_open_utc: datetime | None = None,
        updated_at_utc: datetime | None = None,
    ) -> None:
        now_iso = (updated_at_utc or datetime.now(timezone.utc)).isoformat()
        next_iso = next_open_utc.isoformat() if next_open_utc is not None else ""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO bot_heartbeat
                    (singleton_id, symbol, mode, market_open, status, next_open_utc, updated_at_utc)
                    VALUES (1, ?, ?, ?, ?, ?, ?)
                    """,
                    (symbol.upper(), mode, 1 if market_open else 0, status, next_iso, now_iso),
                )
        finally:
            if self._memory_conn is None:
                conn.close()

    def get_latest_heartbeat(self) -> BotHeartbeat | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT symbol, mode, market_open, status, next_open_utc, updated_at_utc FROM bot_heartbeat WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                return None
            next_dt = (
                datetime.fromisoformat(row["next_open_utc"])
                if row["next_open_utc"]
                else None
            )
            upd_dt = datetime.fromisoformat(row["updated_at_utc"])
            return BotHeartbeat(
                symbol=str(row["symbol"]),
                mode=str(row["mode"]),
                market_open=bool(row["market_open"]),
                status=str(row["status"]),
                next_open_utc=next_dt,
                updated_at_utc=upd_dt,
            )
        finally:
            if self._memory_conn is None:
                conn.close()

    def list_recent_processed_bars(self, limit: int = 100) -> list[dict[str, str]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT symbol, mode, bar_close_iso, action, reason, created_at_utc
                FROM processed_bars
                ORDER BY created_at_utc DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            if self._memory_conn is None:
                conn.close()

    def list_tracked_orders(self, limit: int = 200) -> list[TrackedBotOrder]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM bot_orders
                ORDER BY submitted_at_utc DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            out: list[TrackedBotOrder] = []
            for r in rows:
                out.append(
                    TrackedBotOrder(
                        client_order_id=str(r["client_order_id"]),
                        broker_order_id=str(r["broker_order_id"]),
                        symbol=str(r["symbol"]),
                        mode=str(r["mode"]),
                        side=str(r["side"]),
                        order_type=str(r["order_type"]),
                        qty=int(r["qty"]),
                        reference_price=float(r["reference_price"]),
                        limit_price=float(r["limit_price"]) if r["limit_price"] is not None else None,
                        stop_loss_price=float(r["stop_loss_price"]),
                        take_profit_price=float(r["take_profit_price"]),
                        status=str(r["status"]),
                        session_date=str(r["session_date"]),
                        realized_pnl_usd=float(r["realized_pnl_usd"]),
                        submitted_at_utc=datetime.fromisoformat(r["submitted_at_utc"]),
                    )
                )
            return out
        finally:
            if self._memory_conn is None:
                conn.close()

    def has_processed_bar(self, symbol: str, mode: str, bar_close_et: datetime) -> bool:
        iso_key = bar_close_et.isoformat()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM processed_bars WHERE symbol = ? AND mode = ? AND bar_close_iso = ?",
                (symbol.upper(), mode, iso_key),
            ).fetchone()
            return row is not None
        finally:
            if self._memory_conn is None:
                conn.close()

    def mark_bar_processed(
        self,
        symbol: str,
        mode: str,
        bar_close_et: datetime,
        action: str,
        reason: str,
    ) -> None:
        iso_key = bar_close_et.isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO processed_bars
                    (symbol, mode, bar_close_iso, action, reason, created_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (symbol.upper(), mode, iso_key, action, reason, now_iso),
                )
        finally:
            if self._memory_conn is None:
                conn.close()

    def record_order_submission(
        self,
        *,
        client_order_id: str,
        broker_order_id: str,
        symbol: str,
        mode: str,
        side: str,
        order_type: str,
        qty: int,
        reference_price: float,
        limit_price: float | None,
        stop_loss_price: float,
        take_profit_price: float,
        status: str,
        session_date: date,
        submitted_at_utc: datetime | None = None,
    ) -> None:
        sub_ts = (submitted_at_utc or datetime.now(timezone.utc)).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO bot_orders (
                        client_order_id, broker_order_id, symbol, mode, side, order_type,
                        qty, reference_price, limit_price, stop_loss_price, take_profit_price,
                        status, session_date, realized_pnl_usd, submitted_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?)
                    """,
                    (
                        client_order_id,
                        broker_order_id,
                        symbol.upper(),
                        mode,
                        side,
                        order_type,
                        int(qty),
                        float(reference_price),
                        float(limit_price) if limit_price is not None else None,
                        float(stop_loss_price),
                        float(take_profit_price),
                        status,
                        session_date.isoformat(),
                        sub_ts,
                        sub_ts,
                    ),
                )
        finally:
            if self._memory_conn is None:
                conn.close()

    def update_order_status(
        self,
        client_order_id: str,
        status: str,
        *,
        realized_pnl_usd: float | None = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            with conn:
                if realized_pnl_usd is None:
                    conn.execute(
                        "UPDATE bot_orders SET status = ?, updated_at_utc = ? WHERE client_order_id = ?",
                        (status, now_iso, client_order_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE bot_orders
                        SET status = ?, realized_pnl_usd = ?, updated_at_utc = ?
                        WHERE client_order_id = ?
                        """,
                        (status, float(realized_pnl_usd), now_iso, client_order_id),
                    )
        finally:
            if self._memory_conn is None:
                conn.close()

    def has_active_bot_symbol_session_orders(self, symbol: str, session_date: date) -> bool:
        """Return True if spy-daytrader submitted any non-cancelled order for `symbol` today."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT 1 FROM bot_orders
                WHERE symbol = ? AND session_date = ?
                  AND status NOT IN ('cancelled', 'canceled', 'rejected', 'expired', 'CANCELLED_STALE', 'VETOED')
                LIMIT 1
                """,
                (symbol.upper(), session_date.isoformat()),
            ).fetchone()
            return row is not None
        finally:
            if self._memory_conn is None:
                conn.close()

    def get_session_realized_pnl(self, session_date: date) -> float:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl_usd), 0.0) AS pnl FROM bot_orders WHERE session_date = ?",
                (session_date.isoformat(),),
            ).fetchone()
            return float(row["pnl"]) if row is not None else 0.0
        finally:
            if self._memory_conn is None:
                conn.close()

    def save_dashboard_cache(
        self,
        payload_json: str,
        *,
        updated_at_utc: datetime | None = None,
    ) -> None:
        """Persist non-secret UI snapshot JSON so Streamlit Cloud works from GitHub Secrets alone."""
        now_iso = (updated_at_utc or datetime.now(timezone.utc)).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO dashboard_cache (singleton_id, payload_json, updated_at_utc)
                    VALUES (1, ?, ?)
                    """,
                    (payload_json, now_iso),
                )
        except sqlite3.OperationalError:
            pass
        finally:
            if self._memory_conn is None:
                conn.close()

    def load_dashboard_cache(self) -> tuple[str, datetime] | None:
        """Load persisted UI snapshot JSON written by GitHub Actions or local bot."""
        if str(self.db_path) != ":memory:" and not self.db_path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload_json, updated_at_utc FROM dashboard_cache WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                return None
            return str(row["payload_json"]), datetime.fromisoformat(str(row["updated_at_utc"]))
        except sqlite3.OperationalError:
            return None
        finally:
            if self._memory_conn is None:
                conn.close()
