# Free-Forever Autonomous Live Deployment Guide (`$0/month` Forever — Option A)

`spy-daytrader` is architected for **100% Free-Forever, On-Time Cloud Operation** (`$0/month`, no 30-day trial expiry) using the shared **External Edge Cron -> GitHub `workflow_dispatch` REST API** bridge:

1. **On-Time External Edge Cron (`infrastructure/cloudflare_cron/`) -> GitHub Actions ([`.github/workflows/intraday_session.yml`](./.github/workflows/intraday_session.yml))**:
   - **Zero Cron Lag or Dropped Runs**: Native GitHub `on: schedule` is intentionally disabled. A dedicated DST-aware Cloudflare Worker (`worker.js` / `wrangler.toml`) or `cron-job.org` (`setup_cronjob_org.py`) dispatches `workflow_dispatch` on exact **`America/New_York`** wall-clock time:
     - **Session Part 1 (`Mon–Fri 09:20 ET`)**: `inputs: { action: "session", until_et: "12:58" }`
     - **Session Part 2 (`Mon–Fri 12:50 ET`)**: `inputs: { action: "session", until_et: "16:05" }`
   - **Hardened Midday Handoff (Zero-Kill Overlap)**: Part 1 and Part 2 use separate concurrency groups with `cancel-in-progress: false`. Part 2 never kills Part 1 while booting; instead it passes `pytest -q`, verifies Alpaca health (`python main.py status`), waits for Part 1's clean `12:58 ET` exit & SQLite commit, pulls `data/bot_state.db`, and takes over seamlessly through the `15:55 ET` EOD flatten.
   - **Direct Cloudflare Git Integration (`$0/month`)**: Connect a Cloudflare Worker directly to `jamietan2002/spy-daytrader` (**Root directory**: `infrastructure/cloudflare_cron`, **Deploy command**: `npx wrangler deploy`) and add secret `GITHUB_PAT` (`Actions: Read and write` scoped to `spy-daytrader`). *(Or run `CRONJOB_API_KEY=... GITHUB_PAT=... .venv/bin/python infrastructure/cloudflare_cron/setup_cronjob_org.py`.)*
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
