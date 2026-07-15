"""
Data sources. All loaders return a pandas DataFrame with columns:
    time (pandas datetime64), open, high, low, close, volume
sorted ascending by time, index reset to 0..n-1.

Three sources are supported:

  * MetaTrader5 (get_mt5_data) — the recommended source since most brokers
    that offer both BTCUSD and XAUUSD as tradeable CFD symbols run on
    MT5/MT4. Only works on Windows with the MT5 terminal installed and
    logged in; the `MetaTrader5` package is optional (see requirements.txt).

  * ccxt (get_ccxt_data) — fallback source for BTCUSD specifically, pulling
    BTC/USDT (or another pair you configure) from a crypto exchange. Not a
    perfect proxy for a broker's BTCUSD quote but fine for building/testing
    the strategy logic if you don't have MT5 access.

  * CSV (load_csv) — for backtesting on your own historical data export.
"""
import logging
import pandas as pd

logger = logging.getLogger(__name__)

MT5_TIMEFRAME_MAP = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}

CCXT_TIMEFRAME_MAP = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
}


def get_mt5_data(symbol: str, timeframe: str, n_bars: int = 500) -> pd.DataFrame:
    try:
        import MetaTrader5 as mt5
    except ImportError as e:
        raise RuntimeError(
            "MetaTrader5 package not installed, or not on Windows. "
            "Run `pip install MetaTrader5` on a Windows machine with the "
            "MT5 terminal installed, or use get_ccxt_data for BTCUSD instead."
        ) from e

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")

    try:
        tf_const = getattr(mt5, MT5_TIMEFRAME_MAP[timeframe])
        rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, n_bars)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"MT5 returned no data for {symbol} {timeframe}: {mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.rename(columns={"tick_volume": "volume"})
        return df[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    finally:
        mt5.shutdown()


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
