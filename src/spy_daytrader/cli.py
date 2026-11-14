"""Command-line interface for running, inspecting, flattening, and backtesting the v2 bot."""

from __future__ import annotations

import argparse
import sys

from spy_daytrader.backtest import BacktestReport, generate_synthetic_spy_5m_bars, run_backtest
from spy_daytrader.broker import AlpacaPaperBroker
from spy_daytrader.config import ATRProfile, BotConfig, StrategyMode
from spy_daytrader.data_feed import NY_TZ, MarketDataFeed
from spy_daytrader.engine import IntradayTradingBot
from spy_daytrader.state_store import SQLiteStateLedger


def _format_report_table(reports: list[BacktestReport]) -> str:
    lines = [
        "=" * 114,
        f"{'Symbol':<6} | {'Mode/Profile':<20} | {'Trades (L/S)':<13} | {'AvgRisk':<7} | "
        f"{'Win Rate':<8} | {'PF':<5} | {'Exp (R)':<8} | {'Max DD':<7} | {'Sharpe':<6} | {'Net P&L ($)':<16}",
        "-" * 114,
    ]
    for r in reports:
        prof = "std" if r.atr_profile == ATRProfile.STANDARD else "wide"
        label = f"{r.mode.value}/{prof}"
        ls_str = f"{r.total_trades} ({r.long_trades}/{r.short_trades})"
        lines.append(
            f"{r.symbol:<6} | {label:<20} | {ls_str:<13} | {r.avg_risk_pct_deployed:>5.2f}% | "
            f"{r.win_rate_pct:>6.1f}% | {r.profit_factor:>5.2f} | {r.expectancy_r:>+7.3f}R | "
            f"{r.max_drawdown_pct:>5.2f}% | {r.sharpe_annualized:>6.2f} | "
            f"${r.net_profit_usd:>+10,.2f} ({r.total_return_pct:+.1f}%)"
        )
    lines.append("=" * 114)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spy-bot",
        description="Institutional v2 Intraday Paper Day-Trading Bot & Backtester (alpaca-py + pandas-ta)",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional path to .env file (defaults to .env in working directory)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # 1. Live Boundary-Snapped 1-minute Paper Loop
    p_run = sub.add_parser("run", help="Start the boundary-snapped 1-minute Alpaca Paper trading loop")
    p_run.add_argument("--mode", choices=["alpha_max", "baseline"], default=None)
    p_run.add_argument("--atr-profile", choices=["standard", "wide"], default=None)
    p_run.add_argument("--symbol", default=None, help="Ticker (e.g. SPY, QQQ, TQQQ, NVDA)")
    p_run.add_argument("--dry-run", action="store_true", help="Evaluate signals without submitting orders")

    # 2. Single Evaluation Tick
    p_once = sub.add_parser("once", help="Execute a single evaluation cycle and exit")
    p_once.add_argument("--mode", choices=["alpha_max", "baseline"], default=None)
    p_once.add_argument("--atr-profile", choices=["standard", "wide"], default=None)
    p_once.add_argument("--symbol", default=None)
    p_once.add_argument("--dry-run", action="store_true")

    # 3. Account & Exposure Status
    p_status = sub.add_parser("status", help="Display Alpaca Paper account, clock, ledger, and exposure")
    p_status.add_argument("--symbol", default=None)

    # 4. EOD / Manual Flatten
    p_flat = sub.add_parser("flatten", help="Cancel open orders and flatten open position for symbol")
    p_flat.add_argument("--symbol", default=None)
    p_flat.add_argument(
        "--force-unowned",
        action="store_true",
        help="Also flatten external non-spydt positions on this symbol if present",
    )

    # 5. Historical 5m Backtest & Multi-Symbol / Profile Tournament
    p_bt = sub.add_parser("backtest", help="Backtest 5m bars across modes, ATR profiles, and symbols")
    p_bt.add_argument("--days", type=int, default=90, help="Calendar lookback days of 5m Alpaca bars")
    p_bt.add_argument("--symbol", default=None)
    p_bt.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated list of tickers to benchmark (e.g. SPY,QQQ,TQQQ)",
    )
    p_bt.add_argument("--mode", choices=["alpha_max", "baseline"], default=None)
    p_bt.add_argument("--atr-profile", choices=["standard", "wide"], default=None)
    p_bt.add_argument("--compare", action="store_true", help="Benchmark baseline vs alpha_max (std & wide)")
    p_bt.add_argument("--synthetic", action="store_true", help="Use offline synthetic 5m bars")
    p_bt.add_argument("--slippage-bps", type=float, default=1.0, help="Per-leg adverse slippage in bps")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = BotConfig.from_env(
        env_file=args.env_file,
        mode_override=getattr(args, "mode", None),
        symbol_override=getattr(args, "symbol", None),
        atr_profile_override=getattr(args, "atr_profile", None),
    )

    if args.command == "run":
        bot = IntradayTradingBot(cfg)
        bot.run_forever(dry_run=bool(args.dry_run))
        return 0

    if args.command == "once":
        bot = IntradayTradingBot(cfg)
        outcome = bot.run_once(dry_run=bool(args.dry_run))
        print(f"Cycle Status: {outcome.status}")
        return 0

    if args.command == "status":
        ledger = SQLiteStateLedger(cfg.state_db_path)
        broker = AlpacaPaperBroker(cfg, ledger=ledger)
        acct = broker.get_account_snapshot()
        clock = broker.get_market_clock()
        now_et = clock.timestamp.astimezone(NY_TZ)
        bot_pnl = ledger.get_session_realized_pnl(now_et.date())
        has_exp, exp_desc = broker.has_open_exposure(cfg.symbol)
        print(f"Symbol            : {cfg.symbol} (mode={cfg.strategy_mode.value}, atr_profile={cfg.atr_profile.value})")
        print(f"Paper Equity      : ${acct.equity:,.2f} (Account Daily P&L: ${acct.daily_pnl_usd:+,.2f} / {acct.daily_pnl_pct:+.2%})")
        print(f"Bot Session P&L   : ${bot_pnl:+,.2f} (SQLite Ledger: {cfg.state_db_path})")
        print(f"Buying Power      : ${acct.buying_power:,.2f} | Cash: ${acct.cash:,.2f}")
        print(f"Market Open       : {clock.is_open} (Next Open: {clock.next_open.astimezone(NY_TZ).strftime('%Y-%m-%d %H:%M ET')})")
        print(f"Current Exposure  : {exp_desc} (has_exposure={has_exp})")
        return 0

    if args.command == "flatten":
        ledger = SQLiteStateLedger(cfg.state_db_path)
        broker = AlpacaPaperBroker(cfg, ledger=ledger)
        clock = broker.get_market_clock()
        res = broker.flatten_symbol_eod(
            cfg.symbol,
            session_date=clock.timestamp.astimezone(NY_TZ).date(),
            force_unowned=bool(args.force_unowned),
        )
        print(f"Flatten Result: {res}")
        return 0

    if args.command == "backtest":
        symbols = (
            [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            if args.symbols
            else [cfg.symbol]
        )
        all_reports: list[BacktestReport] = []
        for sym in symbols:
            sym_cfg = cfg.model_copy(update={"symbol": sym})
            if args.synthetic:
                bars = generate_synthetic_spy_5m_bars(sessions=max(20, args.days))
                source_label = f"Synthetic 5m RTH ({len(bars)} bars)"
            else:
                feed = MarketDataFeed(sym_cfg)
                bars = feed.fetch_closed_bars(lookback_days=args.days, tail_bars=0)
                source_label = f"Alpaca {sym_cfg.data_feed.upper()} 5m RTH ({len(bars)} bars over {args.days}d)"

            if args.compare or (args.mode is None and args.atr_profile is None):
                variants = [
                    sym_cfg.model_copy(
                        update={
                            "strategy_mode": StrategyMode.BASELINE,
                            "atr_profile": ATRProfile.STANDARD,
                            "stop_loss_atr_mult": 1.5,
                            "take_profit_atr_mult": 2.25,
                        }
                    ),
                    sym_cfg.model_copy(
                        update={
                            "strategy_mode": StrategyMode.ALPHA_MAX,
                            "atr_profile": ATRProfile.STANDARD,
                            "stop_loss_atr_mult": 1.5,
                            "take_profit_atr_mult": 2.25,
                        }
                    ),
                    sym_cfg.model_copy(
                        update={
                            "strategy_mode": StrategyMode.ALPHA_MAX,
                            "atr_profile": ATRProfile.WIDE,
                            "stop_loss_atr_mult": 2.0,
                            "take_profit_atr_mult": 3.0,
                        }
                    ),
                ]
                for v_cfg in variants:
                    all_reports.append(run_backtest(bars, v_cfg, slippage_bps=args.slippage_bps))
            else:
                all_reports.append(run_backtest(bars, sym_cfg, slippage_bps=args.slippage_bps))

        print(f"\n5-Minute Intraday Backtest — [{source_label}]")
        print(_format_report_table(all_reports))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
