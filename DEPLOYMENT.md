# Free-Forever Autonomous Live Deployment Guide (`$0/month` Forever)

`spy-daytrader` is architected for **100% Free-Forever, Zero-Maintenance Cloud Operation** (no 30-day trial expiry, no credit card required) using the exact same stack as `jamietan2002/trader`:

1. **Autonomous Market-Hours Execution — GitHub Actions ([`.github/workflows/intraday_session.yml`](./.github/workflows/intraday_session.yml))**:
   - **Unlimited Free Forever** on your public GitHub repository (`jamietan2002/spy-daytrader`), with your Alpaca Paper API keys stored strictly in encrypted GitHub Actions Secrets.
   - **Full `09:30–16:00 ET` (`21:30–04:00 SGT`) Coverage**: Two overlap-safe scheduled sessions (`Morning 13:20 UTC` + `Afternoon 16:50 UTC` with `concurrency` handoff) poll closed 5-minute `SPY` bars every 1 minute (`:00:01.5s`), manage Marketable Limit bracket orders on dedicated Alpaca Paper Account #3 (`PA3OSGSIE`), auto-cancel stale entries after 55s, flatten intraday exposure 5 minutes before close (`15:55 ET` or `12:55 ET` on early-close half-days), exit immediately on NYSE holidays, and sync `data/bot_state.db` back to `main`.
2. **Minimalist Matte-Dark 2-Tab Web UI — Streamlit Community Cloud (`dashboard/streamlit_app.py`)**:
   - **Free Forever 24/7 Web & Mobile Dashboard** at [https://share.streamlit.io](https://share.streamlit.io).
   - Streams live Paper equity, open positions, stop-loss / profit-target legs, order fills, and daily P&L directly from Alpaca Paper Account #3 every `30s` alongside the synced `data/bot_state.db` journal.

---

## One-Time Streamlit Community Cloud UI Setup (~1 Minute, Free Forever)

*(Note: `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` for Paper Account #3 are already configured in your GitHub Actions Secrets on `jamietan2002/spy-daytrader`.)*

1. Go to [https://share.streamlit.io](https://share.streamlit.io) and click **Create app**:
   - **Repository**: `jamietan2002/spy-daytrader`
   - **Branch**: `main`
   - **Main file path**: `dashboard/streamlit_app.py`
2. Click **Advanced settings... -> Secrets** and paste your dedicated Paper Account #3 credentials in TOML format:
   ```toml
   ALPACA_API_KEY = "<your_paper_account_3_key>"
   ALPACA_SECRET_KEY = "<your_paper_account_3_secret>"
   ALPACA_DATA_FEED = "iex"
   STRATEGY_MODE = "alpha_max"
   ATR_PROFILE = "standard"
   TRADING_SYMBOL = "SPY"
   ```
3. Click **Deploy!** — you now have a permanent, free-forever HTTPS dashboard showing live trades and daily P&L in Singapore Time (`SGT`).

---

## Alternative Self-Hosted Options

### Local Combined Bot + UI (`python main.py serve`)
```bash
.venv/bin/python main.py serve --port 8501
```

### Docker Compose (Any Local Machine or Free-Tier VM)
```bash
docker compose up --build -d
```
Open **`http://localhost:8501`** (healthcheck: `http://localhost:8501/_stcore/health`).
