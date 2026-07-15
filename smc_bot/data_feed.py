"""
Data sources. All loaders return a pandas DataFrame with columns:
    time (pandas datetime64), open, high, low, close, volume
sorted ascending by time, index reset to 0..n-1.

Two sources are supported:

  * ccxt (get_ccxt_data) — primary live source for BTCUSD, pulling
    BTC/USDT (or another pair you configure) from a crypto exchange.

  * CSV (load_csv) — for backtesting on your own historical data export.

(MT5 support has been removed — this deployment doesn't use it. XAUUSD and
any symbol without a ccxt equivalent falls straight through to TwelveData;
see live_bot.py.)
"""
import logging
import pandas as pd

logger = logging.getLogger(__name__)

CCXT_TIMEFRAME_MAP = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
}


def get_ccxt_data(exchange_id: str, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
    try:
        import ccxt
    except ImportError as e:
        raise RuntimeError("ccxt not installed. Run `pip install ccxt`.") from e

    exchange_cls = getattr(ccxt, exchange_id)
    exchange = exchange_cls({"enableRateLimit": True})
    tf = CCXT_TIMEFRAME_MAP[timeframe]
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    return df.reset_index(drop=True)


def load_csv(path: str) -> pd.DataFrame:
    """
    Expects a CSV with (case-insensitive) columns for time/date, open, high,
    low, close and optionally volume. Common export formats from
    TradingView / MT5 / brokers are auto-detected reasonably well.
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]

    time_col = next((c for c in df.columns if c in ("time", "date", "datetime", "timestamp")), None)
    if time_col is None:
        raise ValueError(f"Could not find a time/date column in {path}. Columns found: {list(df.columns)}")

    df = df.rename(columns={time_col: "time"})
    df["time"] = pd.to_datetime(df["time"])

    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"Missing required column '{col}' in {path}")

    if "volume" not in df.columns:
        df["volume"] = 0

    df = df.sort_values("time").reset_index(drop=True)
    return df[["time", "open", "high", "low", "close", "volume"]]
