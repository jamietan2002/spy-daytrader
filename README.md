# SPY Intraday Day-Trading Bot, Minimalist Dark Hub & 5m Engine

An institutional-grade Python intraday paper-trading bot, Minimalist Matte-Dark 2-Tab Web UI, and chronological 5-minute backtester built on **`alpaca-py`**, **`pandas-ta`**, and **`streamlit`**.

---

## Key Features & Production Safeguards

1. **Always-On Single-Container Live Setup (`python main.py serve`)**:
   - Runs **both** the autonomous US market-hours trading worker thread and the Minimalist Matte-Dark 2-Tab Streamlit UI inside one self-healing process (`Dockerfile`, `railway.toml`, `render.yaml`, `docker-compose.yml`).
   - **Precision Market-Hours Loop (`21:30–04:00 SGT`)**: Wakes boundary-snapped at `:00:01.5s` every minute during NYSE sessions, drops into low-power 5-minute standby outside market hours, and wakes automatically 2 minutes before the next opening bell.
2. **Minimalist Matte-Dark 2-Tab Web UI (`dashboard/streamlit_app.py`)**:
   - **`Today & Open Trade`**: Real-time Paper equity, today's P&L, 30-day realized P&L & win rate, active trade details (`Shares`, `Entry / Current Price`, `Unrealized P&L`, `Auto Stop-Loss`, `Profit Target`, `Mandatory Close` in `SGT`), and today's fills in plain everyday English.
   - **`Daily P&L & Journal`**: Day-by-day P&L history table plus a focused single-day journal (`← Previous` / `Next →`) with a 1-sentence `Daily Takeaway` banner and `Date / Time (SGT)` timestamps.
   - Zero emojis, no left sidebar, no manual sync clutter (auto-refreshes every `30s`).
3. **Hard Paper-Trading Safety Lock & Bot-Scoped OMS Isolation (`spydt-*`)**:
   - Refuses to start unless `ALPACA_PAPER=true` and `ALPACA_BASE_URL=https://paper-api.alpaca.markets` (`LiveTradingProhibitedError`).
   - WAL-mode SQLite state ledger (`data/bot_state.db`) persists heartbeats, processed 5m bars, and `spydt-*` orders, enforcing a `-2.5%` daily drawdown circuit breaker and `15:55 ET` EOD flatten that never touches external account positions.

---

## Quickstart

### 1. Local Setup & Live Hub
```bash
/Library/GoogleCorpSupport/Python/3.11/bin/python3.11 -m venv .venv
.venv/bin/pip install --only-binary :all: -r requirements.txt
.venv/bin/pip install --no-build-isolation --no-deps -e .

cp .env.example .env
# Add your dedicated Alpaca Paper API Key & Secret Key to .env

# Launch BOTH the autonomous market-hours bot AND Minimalist Dark UI on http://localhost:8501
.venv/bin/python main.py serve --port 8501
```

### 2. Cloud Deployment (`Railway` / `Render` / `Docker`)
See [**DEPLOYMENT.md**](./DEPLOYMENT.md) for 2-minute deployment instructions using [`Dockerfile`](./Dockerfile), [`railway.toml`](./railway.toml), [`render.yaml`](./render.yaml), or [`docker-compose.yml`](./docker-compose.yml).

### 3. CLI Reference
```bash
.venv/bin/python main.py serve                  # Run bot worker + Minimalist Dark Web UI ($PORT / 8501)
.venv/bin/python main.py serve --no-bot         # Run Web UI only
.venv/bin/python main.py run                    # Run headless 1-minute Paper bot
.venv/bin/python main.py status                 # Check Paper equity, clock, ledger P&L, & exposure
.venv/bin/python main.py backtest --days 180 --compare
.venv/bin/python main.py flatten                # Cancel bot orders & flatten intraday exposure
```

### 4. Automated Verification Suite
```bash
.venv/bin/pytest -v --cov=spy_daytrader --cov-report=term-missing
```
