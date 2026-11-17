# AGENTS.md — SPY Intraday Day-Trading Bot Engineering & UI Rules

## 1. Non-Negotiable Safety, Quantitative & UI Invariants

1. **Paper Trading Lock (`ALPACA_PAPER=true`)**:
   - `src/spy_daytrader/config.py` enforces `alpaca_paper=True` and `https://paper-api.alpaca.markets`. Any non-paper setting raises `LiveTradingProhibitedError`. Never weaken or bypass this check.
2. **Zero Secret Leakage**:
   - Never log or commit credentials. All loggers must route through `get_logger()` in `src/spy_daytrader/logging_utils.py` (`SecretScrubbingFilter`).
3. **Closed-Bar Causality & Indicator Convergence**:
   - Never evaluate signals on incomplete/forming 5m candles (`bar_open + 5m <= now_et`).
   - Always fetch `>= 250` (default `650`) RTH 5-minute bars so `EMA(200)` is non-NaN and converged.
4. **Bot-Scoped OMS Isolation (`spydt-*` + `SQLiteStateLedger`)**:
   - Every order must carry a deterministic `spydt-*` `client_order_id` and be recorded in `data/bot_state.db`.
   - EOD flatten (`15:55 ET` or `<= 5m` before early-close half-days) must cancel child bracket orders before calling `close_position` and must never liquidate external positions not originated by `spydt-*` unless `--force-unowned` is explicitly invoked by the operator.
5. **Minimalist Matte-Dark UI Standards (`dashboard/streamlit_app.py` & `dashboard_data.py`)**:
   - Enforce `#0c0e12` matte-dark canvas, `#13161c` cards, tabular numerals, zero emojis, no left sidebar, green (`#22c55e`) / red (`#ef4444`) strictly on financial outcomes, plain-English summaries with zero raw quant jargon, and explicit `Date / Time (SGT)` timestamps.

## 2. Module Map

| File | Responsibility |
|---|---|
| `main.py` / `src/spy_daytrader/cli.py` | CLI (`session`, `serve`, `run`, `once`, `status`, `flatten`, `backtest`) |
| `.github/workflows/intraday_session.yml` | Free-forever `workflow_dispatch` Morning (`09:20 ET`) + Afternoon (`12:50 ET`) NYSE market-hours GitHub Actions runner (triggered via External Edge Cron) & SQLite sync |
| `src/spy_daytrader/supervisor.py` | Single-container Cloud Supervisor (background bot thread + Streamlit `$PORT` server) |
| `dashboard/streamlit_app.py` | Minimalist Matte-Dark 2-Tab UI (`Today & Open Trade`, `Daily P&L & Journal`) |
| `src/spy_daytrader/live_stream.py` | Event-driven Alpaca `TradingStream` WebSocket listener & `@st.cache_resource` RAM snapshot hub |
| `src/spy_daytrader/dashboard_data.py` | Live Alpaca + SQLite plain-English telemetry, round-trip P&L, & SGT formatting |
| `src/spy_daytrader/config.py` | Frozen Pydantic config, `StrategyMode`, `ATRProfile`, Paper-mode lock |
| `src/spy_daytrader/data_feed.py` | Alpaca 5m RTH closed-bar feed (`IEX`/`SIP` auto-fallback) & live NBBO quote anchor |
| `src/spy_daytrader/indicators.py` | `pandas-ta` `EMA(200)`, `EMA(50)`, `StochRSI(14,14,3,3)`, `ATR(14)`, daily `Session VWAP` |
| `src/spy_daytrader/strategy.py` | `BASELINE` & `ALPHA_MAX` v2 session-regime signal engine |
| `src/spy_daytrader/risk.py` | `1%` equity risk sizing, buying-power cap, Marketable Limit collar, circuit breakers |
| `src/spy_daytrader/state_store.py` | WAL-mode SQLite persistence (`bot_heartbeat`, `processed_bars`, `bot_orders`) |
| `src/spy_daytrader/broker.py` | Alpaca `TradingClient` adapter, stale limit auto-cancel, isolated EOD flatten |
| `src/spy_daytrader/engine.py` | Boundary-snapped (`:00:01.5s`) 1m execution loop, `run_market_session`, & off-hours standby |
| `src/spy_daytrader/backtest.py` | Event-driven 5m chronological backtester with limit-collar & slippage physics |
| `Dockerfile` / `render.yaml` / `docker-compose.yml` | Optional self-hosted / free-tier Docker manifests |

## 3. Mandatory Verification Before Committing
Always run:
```bash
.venv/bin/pytest -v --cov=spy_daytrader --cov-report=term-missing
```
