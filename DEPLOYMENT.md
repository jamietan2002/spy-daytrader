# Free-Forever Autonomous Live Deployment Guide (`$0/month` Forever)

`spy-daytrader` is architected for **100% Free-Forever, Zero-Maintenance Cloud Operation** (no 30-day trial expiry, no credit card required) using the exact same stack as `jamietan2002/trader`:

1. **Autonomous Market-Hours Execution — GitHub Actions ([`.github/workflows/intraday_session.yml`](./.github/workflows/intraday_session.yml))**:
   - **Unlimited Free Forever** on your public GitHub repository (`jamietan2002/spy-daytrader`), with your Alpaca Paper API keys stored strictly in encrypted GitHub Actions Secrets.
   - **Full `09:30–16:00 ET` (`21:30–04:00 SGT`) Coverage**: Two overlap-safe scheduled sessions (`Morning 13:20 UTC` + `Afternoon 16:50 UTC` with `concurrency` handoff) poll closed 5-minute `SPY` bars every 1 minute (`:00:01.5s`), manage Marketable Limit bracket orders on dedicated Alpaca Paper Account #3 (`PA3OSGSIE`), auto-cancel stale entries after 55s, flatten intraday exposure 5 minutes before close (`15:55 ET` or `12:55 ET` on early-close half-days), exit immediately on NYSE holidays, and sync `data/bot_state.db` back to `main`.
2. **Minimalist Matte-Dark 2-Tab Web UI — Streamlit Community Cloud (`dashboard/streamlit_app.py` + `src/spy_daytrader/live_stream.py`)**:
   - **Free Forever 24/7 Web & Mobile Dashboard** at [https://share.streamlit.io](https://share.streamlit.io).
   - Connects a persistent background `TradingStream` WebSocket (`wss://paper-api.alpaca.markets/stream`) inside a `@st.cache_resource` singleton (`BrokerTradeStreamHub`), serving UI clicks from RAM in `<0.5 ms` and updating automatically whenever Alpaca pushes an actual order/fill event (with zero mid-session Git commits).

---

## One-Time Streamlit Community Cloud UI Setup

1. Go to [https://share.streamlit.io](https://share.streamlit.io) and click **Create app**:
   - **Repository**: `jamietan2002/spy-daytrader`
   - **Branch**: `main`
   - **Main file path**: `dashboard/streamlit_app.py`
2. **Enable Event-Driven Live Fill Streaming (Recommended)**:
   Open **App Settings -> Secrets** in Streamlit Community Cloud and paste your dedicated Paper Account #3 keys in TOML format:
   ```toml
   ALPACA_API_KEY = "<your-paper-account-3-key>"
   ALPACA_SECRET_KEY = "<your-paper-account-3-secret>"
   ```
   Once saved, `BrokerTradeStreamHub` subscribes directly to Alpaca's `trade_updates` WebSocket stream so real fills, cancellations, and bracket exits appear automatically with zero HTTP polling lag.
3. *(Zero-Secret Fallback)* If Streamlit Secrets are left blank, the dashboard still loads cleanly from the end-of-session GitHub-synced `data/bot_state.db` ledger (`mtime`-cached in RAM).

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
