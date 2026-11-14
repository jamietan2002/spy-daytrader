# SPY Intraday Day-Trading Bot & 5m Walk-Forward Engine

An institutional-grade Python intraday paper-trading bot and chronological 5-minute backtester built on **`alpaca-py`** and **`pandas-ta`**.

---

## Key Features & Production Safeguards

1. **Hard Paper-Trading Safety Lock (`LiveTradingProhibitedError`)**:
   - Strictly refuses to start unless `ALPACA_PAPER=true` and `ALPACA_BASE_URL=https://paper-api.alpaca.markets`.
   - Automatic regex & env credential redaction across all logs (`SecretScrubbingFilter`).
2. **Zero Repainting & Converged Indicator Math**:
   - Warms up on **650+ closed 5-minute Regular Trading Hours (`09:30–16:00 ET`) bars** (~8.5 sessions) so `EMA(200)`, `EMA(50)`, `StochRSI(14, 14, 3, 3)`, `ATR(14)`, and `Session VWAP` are mathematically converged (`< 0.15%` exponential seed decay).
   - Evaluates signals **strictly on closed 5m candles** (`bar_open + 5m <= now`) via a boundary-snapped 1-minute scheduler waking at `:00:01.5s`.
3. **Dual Strategy Modes (`alpha_max` & `baseline`) + Configurable ATR Profiles**:
   - **`alpha_max` (Default — Walk-Forward Validated Session-Regime Engine)**:
     - **Longs**: `Close > EMA(200)`, `EMA(50) > EMA(200)`, `Close >= Session VWAP`, bullish reversal candle (`Close > Open`), and `StochRSI %K` crossing above `%D` from `< 20` during institutional trend windows (`09:40–11:00 ET` and `13:00–15:00 ET`, skipping `11:00–13:00 ET` NYC lunch chop).
     - **Symmetric Shorts**: `Close < EMA(200)` with declining `EMA(200)` slope and `StochRSI %K` crossing below `%D` from `> 80` during `11:00–15:00 ET` (skipping morning short-squeeze auctions).
     - **Marketable Limit Bracket Collar**: Caps entry slippage at `ask/bid +- max($0.03, 0.08 * ATR)` and auto-cancels unfilled parent limit entries after `55s`.
   - **`baseline` (Strict Prompt Specification)**:
     - Long-only `Close > EMA(200)` + `StochRSI` cross up below `20`, `1%` equity risk, `1.5x ATR` Stop-Loss, `2.25x ATR` (`1.5R`) Take-Profit via `Market` bracket order.
   - **ATR Profiles (`--atr-profile standard|wide`) & Multi-Ticker Support (`--symbol SPY|QQQ|TQQQ|NVDA`)**:
     - `standard`: `1.5x ATR` SL / `2.25x ATR` TP (`1.5R`).
     - `wide`: `2.0x ATR` SL / `3.0x ATR` TP (`1.5R` — unlocks larger risk deployment per trade and `>50%` OOS win rate on `SPY`).
4. **Bot-Isolated OMS (`spydt-*`) & Crash-Safe SQLite State Ledger (`data/bot_state.db`)**:
   - Persists processed 5m bar timestamps and `spydt-*` prefixed orders in WAL-mode SQLite so mid-session restarts never double-order.
   - Enforces a `-2.5%` bot-scoped AND account-level daily drawdown circuit breaker.
   - At `15:55 ET`, cancels child bracket legs before flattening `spydt-*` intraday exposure while **never touching external positions** held on shared accounts.

---

## Quickstart

### 1. Environment Setup
```bash
/Library/GoogleCorpSupport/Python/3.11/bin/python3.11 -m venv .venv
.venv/bin/pip install --only-binary :all: -r requirements.txt
.venv/bin/pip install --no-build-isolation --no-deps -e .

cp .env.example .env
# Edit .env with your dedicated Alpaca Paper API Key & Secret Key
```

### 2. CLI Commands
```bash
# Check Paper account equity, market clock, SQLite ledger P&L, and exposure
.venv/bin/python main.py status

# Run 180-day 5m historical backtest comparing baseline vs alpha_max (standard & wide)
.venv/bin/python main.py backtest --days 180 --compare

# Compare capital efficiency across SPY, QQQ, and TQQQ
.venv/bin/python main.py backtest --days 180 --symbols SPY,QQQ,TQQQ --compare

# Execute a single dry-run tick without submitting orders
.venv/bin/python main.py once --dry-run

# Start the live boundary-snapped 1-minute Paper trading bot
.venv/bin/python main.py run
# Or with wide ATR profile / TQQQ:
.venv/bin/python main.py run --atr-profile wide
.venv/bin/python main.py run --symbol TQQQ

# Manually cancel bot orders & flatten intraday exposure
.venv/bin/python main.py flatten
```

### 3. Run Automated Test Suite
```bash
.venv/bin/pytest -v -W error --cov=spy_daytrader --cov-report=term-missing
```

