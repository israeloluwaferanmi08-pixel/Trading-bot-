#!/usr/bin/env python3
"""
Run a backtest against your own historical CSV data.

Usage:
    python run_backtest.py --symbol XAUUSD --ltf-csv data/xauusd_m15.csv --htf-csv data/xauusd_h4.csv
    python run_backtest.py --symbol BTCUSD --ltf-csv data/btcusd_m15.csv --htf-csv data/btcusd_h4.csv --plot

CSV files need at least: time/date, open, high, low, close columns.
LTF and HTF CSVs must cover the same underlying symbol/period (HTF = a
higher timeframe of the same instrument, e.g. LTF=M15, HTF=H4).
"""
import argparse
import sys

from smc_bot import config
from smc_bot.data_feed import load_csv
from smc_bot.backtester import Backtester
from smc_bot.metrics import summarize, print_summary


def main():
    parser = argparse.ArgumentParser(description="Backtest the S/D + Premium/Discount strategy.")
    parser.add_argument("--symbol", required=True, choices=list(config.SYMBOLS.keys()))
    parser.add_argument("--ltf-csv", required=True, help="Path to lower-timeframe OHLCV CSV")
    parser.add_argument("--htf-csv", required=True, help="Path to higher-timeframe OHLCV CSV")
    parser.add_argument("--max-open-trades", type=int, default=1)
    parser.add_argument("--plot", action="store_true", help="Save an equity curve PNG next to the CSV")
    args = parser.parse_args()

    sym_cfg = config.SYMBOLS[args.symbol]

    df_ltf = load_csv(args.ltf_csv)
    df_htf = load_csv(args.htf_csv)
    print(f"Loaded {len(df_ltf)} LTF bars ({df_ltf['time'].min()} -> {df_ltf['time'].max()})")
    print(f"Loaded {len(df_htf)} HTF bars ({df_htf['time'].min()} -> {df_htf['time'].max()})")

    strategy_params = config.get_strategy_params(args.symbol)
    bt = Backtester(args.symbol, strategy_params, config.BACKTEST, pip_size=sym_cfg.pip_size)
    result = bt.run(df_ltf, df_htf, max_open_trades=args.max_open_trades)

    print_summary(result, symbol=args.symbol)

    if args.plot:
        try:
            import matplotlib.pyplot as plt
            balances = [e["balance"] for e in result["equity_curve"]]
            plt.figure(figsize=(10, 5))
            plt.plot(balances)
            plt.title(f"{args.symbol} equity curve")
            plt.xlabel("Closed trade #")
            plt.ylabel("Balance")
            out_path = f"{args.symbol}_equity_curve.png"
            plt.savefig(out_path, dpi=120, bbox_inches="tight")
            print(f"Saved equity curve to {out_path}")
        except ImportError:
            print("matplotlib not installed — skipping plot. `pip install matplotlib` to enable --plot.")


if __name__ == "__main__":
    sys.exit(main())
