# Autonomous Live Deployment Guide (`Railway` / `Render` / `Docker` / `Streamlit Cloud`)

`spy-daytrader` runs **both** the autonomous US market-hours trading worker and the Minimalist Matte-Dark 2-Tab Web UI inside **one lightweight process** (`python main.py serve`):

- **During US Market Hours (`Mon–Fri 09:30–16:00 ET` / `21:30–04:00 SGT`)**: Wakes boundary-snapped at `:00:01.5s` every minute, evaluates closed 5-minute bars, manages Marketable Limit brackets, cancels unfilled stale entries after 55s, and flattens bot-owned exposure at `15:55 ET`.
- **Outside Market Hours (Nights / Weekends / Holidays)**: Automatically drops into low-power standby (checking market clock once every 5 minutes and waking 2 minutes before the next NYSE open).
- **Zero Lost History Across Redeploys**: Live positions, working child exits, historical order fills, and daily P&L are reconciled directly from Alpaca's Paper API alongside the local SQLite ledger (`data/bot_state.db`).

---

## Option 1: Deploy to Railway (Recommended Private Repo Setup — ~2 Minutes)

1. Go to [https://railway.com/new](https://railway.com/new) -> **Deploy from GitHub repo** -> select **`jamietan2002/spy-daytrader`** (Private).
2. Railway automatically detects [`railway.toml`](./railway.toml) and [`Dockerfile`](./Dockerfile) (`CMD ["python", "main.py", "serve"]` + `/_stcore/health` healthcheck).
3. Under **Variables**, add your dedicated Alpaca Paper credentials (Slot #3):
   - `ALPACA_API_KEY` = `<your_paper_slot_3_key>`
   - `ALPACA_SECRET_KEY` = `<your_paper_slot_3_secret>`
   - `ALPACA_PAPER` = `true`
   - `STRATEGY_MODE` = `alpha_max`
   - `ATR_PROFILE` = `standard` *(or `wide` for 2.0x/3.0x ATR)*
   - `TRADING_SYMBOL` = `SPY`
4. *(Optional)* Attach a Railway Volume mounted at `/app/data` so `bot_state.db` persists across container rebuilds.
5. Click **Generate Domain** under **Settings -> Networking** to get your live HTTPS dashboard URL (protect with Railway App Sleeping = Off so the background bot runs every market session).

---

## Option 2: Deploy to Render (`render.yaml` Blueprint)

1. Go to [https://dashboard.render.com/blueprints](https://dashboard.render.com/blueprints) -> **New Blueprint Instance** -> connect **`jamietan2002/spy-daytrader`**.
2. Render reads [`render.yaml`](./render.yaml) and prompts only for `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
3. Click **Apply** — your live bot + Minimalist Dark UI starts automatically.

---

## Option 3: Docker Compose (Any VPS or Local Server)

```bash
docker compose up --build -d
```
Open **`http://localhost:8501`** (healthcheck: `http://localhost:8501/_stcore/health`).

---

## Option 4: Local Combined Bot + UI Command

```bash
.venv/bin/python main.py serve --port 8501
```
*(Pass `--no-bot` if you want to view only the UI locally while the bot runs in the cloud.)*

---

## Option 5: Standalone UI on Streamlit Community Cloud (`share.streamlit.io`)

You can also point [https://share.streamlit.io](https://share.streamlit.io) directly at `jamietan2002/spy-daytrader` -> `dashboard/streamlit_app.py` and paste `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` into **App Settings -> Secrets** for a free mobile/desktop dashboard mirror.
