#!/usr/bin/env python3
"""
Generates synthetic-but-plausible OHLCV CSVs (M15 + H4) so you can test-drive
run_backtest.py before plugging in real broker data. This is NOT real market
data — it's a regime-switching random walk used purely to exercise the code
paths (zone detection, premium/discount, trend filter, trade management).
Do not draw any conclusions about real strategy performance from it.
"""
import numpy as np
import pandas as pd


def generate(symbol: str, start_price: float, n_m15_bars: int, vol: float, seed: int):
    rng = np.random.default_rng(seed)
    prices = [start_price]

    # regime-switching drift to create trending legs (needed for zones/impulses to exist)
    regime_len = 0
    drift = 0.0
    for i in range(n_m15_bars):
        if regime_len <= 0:
            regime_len = rng.integers(20, 80)
            drift = rng.choice([-1, 1]) * rng.uniform(0.15, 0.6) * vol
        regime_len -= 1
        shock = rng.normal(0, vol)
        prices.append(max(0.01, prices[-1] + drift + shock))

    prices = np.array(prices)
    times = pd.date_range("2024-01-01", periods=len(prices), freq="15min")

    opens = prices[:-1]
    closes = prices[1:]
    highs = np.maximum(opens, closes) + rng.uniform(0, vol, size=len(opens))
    lows = np.minimum(opens, closes) - rng.uniform(0, vol, size=len(opens))
    volume = rng.integers(100, 1000, size=len(opens))

    df_m15 = pd.DataFrame({
        "time": times[:-1],
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volume,
    })

    # build H4 by resampling the M15 series (16 bars per H4 candle)
    df_h4 = (
        df_m15.set_index("time")
        .resample("4h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
        .reset_index()
    )

    return df_m15, df_h4


if __name__ == "__main__":
    import os
    os.makedirs("data", exist_ok=True)

    m15, h4 = generate("XAUUSD", start_price=2000.0, n_m15_bars=8000, vol=1.2, seed=42)
    m15.to_csv("data/xauusd_m15.csv", index=False)
    h4.to_csv("data/xauusd_h4.csv", index=False)
    print(f"XAUUSD: {len(m15)} M15 bars, {len(h4)} H4 bars written to data/")

    m15, h4 = generate("BTCUSD", start_price=60000.0, n_m15_bars=8000, vol=45.0, seed=7)
    m15.to_csv("data/btcusd_m15.csv", index=False)
    h4.to_csv("data/btcusd_h4.csv", index=False)
    print(f"BTCUSD: {len(m15)} M15 bars, {len(h4)} H4 bars written to data/")
