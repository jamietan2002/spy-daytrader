# SPY Intraday Day-Trading Bot, Minimalist Dark Hub & 5m Engine

An institutional-grade Python intraday paper-trading bot, Minimalist Matte-Dark 2-Tab Web UI, and chronological 5-minute backtester built on **`alpaca-py`**, **`pandas-ta`**, and **`streamlit`**.

---

## Key Features & Production Safeguards

1. **100% Free-Forever Autonomous Hosting (`GitHub Actions` + `Streamlit Community Cloud`)**:
   - **Market-Hours Session Runner ([`.github/workflows/intraday_session.yml`](./.github/workflows/intraday_session.yml))**: Two overlap-safe scheduled workflows (`Morning 13:20 UTC` + `Afternoon 16:50 UTC` with `concurrency` handoff) wake boundary-snapped at `:00:01.5s` every minute across the full `09:30–16:00 ET` (`21:30–04:00 SGT`) NYSE session, exit immediately on holidays, flatten 5 minutes before close (`15:55 ET` or `12:55 ET` on early-close half-days), and sync `data/bot_state.db` back to `main` — `$0/month` forever with no 30-day trial expiry.
   - **Single-Container Local / Docker Supervisor (`python main.py serve`)**: Can also run both the background bot worker and Streamlit UI in one self-healing process locally or via Docker.
2. **Minimalist Matte-Dark 2-Tab Web UI (`dashboard/streamlit_app.py`)**:
   - **`Today & Open Trade`**: Real-time Paper equity, today's P&L, 30-day realized P&L & win rate, active trade details (`Shares`, `Entry / Current Price`, `Unrealized P&L`, `Auto Stop-Loss`, `Profit Target`, `Mandatory Close` in `SGT`), and today's fills in plain everyday English.
   - **`Daily P&L & Journal`**: Day-by-day P&L history table plus a focused single-day journal (`← Previous` / `Next →`) with a 1-sentence `Daily Takeaway` banner and `Date / Time (SGT)` timestamps.
   - Zero emojis, no left sidebar, no manual sync clutter (auto-refreshes every `30s`).
3. **Hard Paper-Trading Safety Lock & Dedicated Account Isolation (`spydt-*`)**:
   - Refuses to start unless `ALPACA_PAPER=true` and `ALPACA_BASE_URL=https://paper-api.alpaca.markets` (`LiveTradingProhibitedError`).
   - Dedicated to isolated Alpaca Paper Account #3 (`PA3OSGSIE`) with WAL-mode SQLite ledger (`data/bot_state.db`), `-2.5%` daily drawdown circuit breaker, and `15:55 ET` EOD flatten.

---

## Quickstart

### 1. Free-Forever Cloud Setup (`GitHub Actions` + `Streamlit Community Cloud`)
See [**DEPLOYMENT.md**](./DEPLOYMENT.md) for the 1-minute Streamlit Community Cloud setup (`share.streamlit.io`).

### 2. Local Setup & Live Hub
```bash
/Library/GoogleCorpSupport/Python/3.11/bin/python3.11 -m venv .venv
.venv/bin/pip install --only-binary :all: -r requirements.txt
.venv/bin/pip install --no-build-isolation --no-deps -e .

cp .env.example .env
# Add your dedicated Alpaca Paper API Key & Secret Key to .env

# Launch BOTH the autonomous market-hours bot AND Minimalist Dark UI on http://localhost:8501
.venv/bin/python main.py serve --port 8501
```

### 3. CLI Reference
```bash
.venv/bin/python main.py session --until-et 16:05  # Bounded NYSE session runner (GitHub Actions)
.venv/bin/python main.py serve                     # Run bot worker + Minimalist Dark Web UI ($PORT / 8501)
.venv/bin/python main.py run                       # Run continuous 24/7 headless loop
.venv/bin/python main.py status                    # Check Paper equity, clock, ledger P&L, & exposure
.venv/bin/python main.py backtest --days 180 --compare
.venv/bin/python main.py flatten                   # Cancel bot orders & flatten intraday exposure
```

### 4. Automated Verification Suite
```bash
.venv/bin/pytest -v --cov=spy_daytrader --cov-report=term-missing
```
