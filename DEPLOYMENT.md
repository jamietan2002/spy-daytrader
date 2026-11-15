# Free-Forever Autonomous Live Deployment Guide (`$0/month` Forever)

`spy-daytrader` is architected for **100% Free-Forever, Zero-Maintenance Cloud Operation** (no 30-day trial expiry, no credit card required) using the exact same stack as `jamietan2002/trader`:

1. **Autonomous Market-Hours Execution — GitHub Actions ([`.github/workflows/intraday_session.yml`](./.github/workflows/intraday_session.yml))**:
   - **Unlimited Free Forever** on your public GitHub repository (`jamietan2002/spy-daytrader`), with your Alpaca Paper API keys stored strictly in encrypted GitHub Actions Secrets.
   - **Full `09:30–16:00 ET` (`21:30–04:00 SGT`) Coverage**: Two overlap-safe scheduled sessions (`Morning 13:20 UTC` + `Afternoon 16:50 UTC` with `concurrency` handoff) poll closed 5-minute `SPY` bars every 1 minute (`:00:01.5s`), manage Marketable Limit bracket orders on dedicated Alpaca Paper Account #3 (`PA3OSGSIE`), auto-cancel stale entries after 55s, flatten intraday exposure 5 minutes before close (`15:55 ET` or `12:55 ET` on early-close half-days), exit immediately on NYSE holidays, and sync `data/bot_state.db` back to `main`.
2. **Minimalist Matte-Dark 2-Tab Web UI — Streamlit Community Cloud (`dashboard/streamlit_app.py`)**:
   - **Free Forever 24/7 Web & Mobile Dashboard** at [https://share.streamlit.io](https://share.streamlit.io).
   - Streams live Paper equity, open positions, stop-loss / profit-target legs, order fills, and daily P&L directly from Alpaca Paper Account #3 every `30s` alongside the synced `data/bot_state.db` journal.

---

## One-Time Streamlit Community Cloud UI Setup (~30 Seconds, Zero Extra Secrets Required)

Because **GitHub Actions** (which already holds your `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in GitHub Secrets) syncs the non-secret portfolio, open-trade, order-journal, and daily-P&L snapshot directly into `data/bot_state.db` on `main` (on every trade event, every 15 minutes during market hours, and at session close — identical to `jamietan2002/trader`), **Streamlit Community Cloud works out-of-the-box with zero Streamlit Secrets needed**:

1. Go to [https://share.streamlit.io](https://share.streamlit.io) and click **Create app**:
   - **Repository**: `jamietan2002/spy-daytrader`
   - **Branch**: `main`
   - **Main file path**: `dashboard/streamlit_app.py`
2. Click **Deploy!** — your dashboard immediately reads the synced `data/bot_state.db` ledger with zero setup.
3. *(Optional)* If you ever want direct `30-second` live broker polling without waiting for GitHub Actions commits, you can also paste `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` into Streamlit Cloud's **App Settings -> Secrets**.

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
