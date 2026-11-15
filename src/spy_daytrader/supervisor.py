"""Single-container Cloud Supervisor running both the autonomous bot thread and Streamlit UI."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from spy_daytrader.config import BotConfig
from spy_daytrader.engine import IntradayTradingBot
from spy_daytrader.logging_utils import get_logger


def run_live_hub(
    config: BotConfig,
    *,
    port: int | None = None,
    host: str = "0.0.0.0",
    enable_bot: bool = True,
    dry_run: bool = False,
) -> int:
    """
    Launch a single self-contained cloud process (ideal for Railway, Render, Fly.io, or Docker):
      1. Background daemon thread running `IntradayTradingBot.run_forever` with automatic
         crash-recovery backoff (wakes every 1m during US market hours, sleeps quietly off-hours).
      2. Foreground Streamlit web server serving `dashboard/streamlit_app.py` on `$PORT`.
      3. Clean SIGINT / SIGTERM forwarding so deployments shut down gracefully within seconds.
    """
    logger = get_logger("spy_daytrader.supervisor")
    resolved_port = int(port or int(os.getenv("PORT", "8501")))
    repo_root = Path(__file__).resolve().parents[2]
    dashboard_path = repo_root / "dashboard" / "streamlit_app.py"
    if not dashboard_path.exists():
        raise FileNotFoundError(f"Dashboard entrypoint not found at {dashboard_path}")

    stop_event = threading.Event()
    bot_instance: IntradayTradingBot | None = None

    def _bot_worker() -> None:
        nonlocal bot_instance
        while not stop_event.is_set():
            try:
                bot_instance = IntradayTradingBot(config)
                bot_instance.run_forever(dry_run=dry_run, register_signals=False)
                break
            except Exception as exc:
                logger.exception(
                    "Bot worker encountered error; restarting in 10s: %s", exc
                )
                stop_event.wait(10.0)

    if enable_bot:
        logger.info(
            "Starting background market-hours bot thread (symbol=%s, mode=%s, dry_run=%s)",
            config.symbol,
            config.strategy_mode.value,
            dry_run,
        )
        worker = threading.Thread(target=_bot_worker, name="spy-bot-worker", daemon=True)
        worker.start()
    else:
        logger.info("Starting Web UI only (--no-bot specified).")

    streamlit_cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(dashboard_path),
        "--server.port",
        str(resolved_port),
        "--server.address",
        host,
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    logger.info("Launching Minimalist Dark UI on http://%s:%d", host, resolved_port)
    ui_proc = subprocess.Popen(streamlit_cmd, cwd=str(repo_root))

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("Supervisor received signal %s; shutting down bot & UI...", signum)
        stop_event.set()
        if bot_instance is not None:
            bot_instance.request_stop()
        if ui_proc.poll() is None:
            ui_proc.terminate()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while ui_proc.poll() is None:
            time.sleep(0.5)
        return int(ui_proc.returncode or 0)
    finally:
        stop_event.set()
        if bot_instance is not None:
            bot_instance.request_stop()
        if ui_proc.poll() is None:
            ui_proc.terminate()
            try:
                ui_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                ui_proc.kill()

