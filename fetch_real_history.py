"""
Fetch REAL historical candles from TwelveData and save as a CSV in the
same format the vision backtest script expects (time,open,high,low,close,volume).

This exists so you don't need MT5 desktop's History Center export --
useful if you're working from a machine without MT5 installed, or just
want real market data quickly using the same TwelveData keys the live
bot already uses.

SETUP
-----
    pip install pandas requests

    export TWELVEDATA_API_KEY="one of your keys"

USAGE
-----
    python fetch_real_history.py --symbol XAU/USD --interval 4h --outsize 500 --out xauusd_h4_real.csv
    python fetch_real_history.py --symbol BTC/USD --interval 15min --outsize 500 --out btcusd_m15_real.csv

NOTE: TwelveData's free tier limits how many bars you can pull per
request (outsize) and how many requests per minute/day -- if you hit a
rate limit, wait a minute and retry, or use a different one of your
three keys.
"""
import argparse
import os
import sys

import pandas as pd
import requests

BASE_URL = "https://api.twelvedata.com/time_series"


def fetch(symbol: str, interval: str, outsize: int, api_key: str) -> pd.DataFrame:
    resp = requests.get(
        BASE_URL,
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": outsize,
            "apikey": api_key,
            "order": "ASC",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "error":
        raise RuntimeError(f"TwelveData error: {data.get('message')}")

    values = data.get("values")
    if not values:
        raise RuntimeError(f"No data returned. Full response: {data}")

    df = pd.DataFrame(values)
    df = df.rename(columns={"datetime": "time"})
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    if "volume" in df.columns:
        df["volume"] = df["volume"].astype(float)
    else:
        df["volume"] = 0.0

    return df[["time", "open", "high", "low", "close", "volume"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, help="e.g. XAU/USD or BTC/USD")
    ap.add_argument("--interval", required=True, help="e.g. 15min, 1h, 4h")
    ap.add_argument("--outsize", type=int, default=500, help="Number of bars to fetch")
    ap.add_argument("--out", required=True, help="Output CSV filename")
    args = ap.parse_args()

    api_key = os.environ.get("TWELVEDATA_API_KEY")
    if not api_key:
        print("Set TWELVEDATA_API_KEY as an environment variable first.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching {args.outsize} bars of {args.symbol} @ {args.interval}...")
    df = fetch(args.symbol, args.interval, args.outsize, api_key)
    df.to_csv(args.out, index=False)
    print(f"Saved {len(df)} real candles to {args.out}")
    print(f"Date range: {df['time'].iloc[0]} to {df['time'].iloc[-1]}")


if __name__ == "__main__":
    main()
