# Free-Forever Autonomous Live Deployment Guide (`$0/month` Forever — Option A)

`spy-daytrader` is architected for **100% Free-Forever, On-Time Cloud Operation** (`$0/month`, no 30-day trial expiry) using the shared **External Edge Cron -> GitHub `workflow_dispatch` REST API** bridge:

1. **On-Time External Edge Cron (`jamietan2002/trader/infrastructure/cloudflare_cron/`) -> GitHub Actions ([`.github/workflows/intraday_session.yml`](./.github/workflows/intraday_session.yml))**:
   - **Zero Cron Lag or Dropped Runs**: Native GitHub `on: schedule` is intentionally disabled so 2–8h GitHub queue delays never miss the `09:30 ET` open or accidentally cancel an active session. Instead, a single DST-aware Cloudflare Worker Cron Trigger (or `cron-job.org`) dispatches `workflow_dispatch` on exact **`America/New_York`** wall-clock time:
     - **Session Part 1 (`Mon–Fri 09:20 ET`)**: `inputs: { action: "session", until_et: "12:58" }`
     - **Session Part 2 (`Mon–Fri 12:50 ET`)**: `inputs: { action: "session", until_et: "16:05" }` (seamless `concurrency` midday handoff)
   - **Full `09:30–16:00 ET` Coverage**: Polls closed 5-minute `SPY` bars every 1 minute (`:00:01.5s`) on dedicated Alpaca Paper Account #3 (`PA3OSGSIE`), auto-cancels stale entries after 55s, flattens intraday exposure 5 minutes before close (`15:55 ET` or `12:55 ET` on early-close half-days), exits immediately on NYSE holidays, and syncs `data/bot_state.db` back to `main`.
   - *(Setup)* One single Cloudflare Worker (or `setup_cronjob_org.py` run) inside `jamietan2002/trader/infrastructure/cloudflare_cron/` dispatches **both** `trader` and `spy-daytrader`. See `trader/DEPLOYMENT.md` for the 3-minute `GITHUB_PAT` activation steps.
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
