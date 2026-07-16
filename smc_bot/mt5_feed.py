"""
MetaTrader5 connection, symbol resolution, and OHLCV data fetching.

This module is Windows-only (the `MetaTrader5` PyPI package requires a
running MT5 terminal on the same machine, which only exists on Windows).
It is not imported anywhere at module load time by the rest of the
package, so the Linux/Railway deployment (live_bot.py) is unaffected --
only mt5_live_bot.py (the Windows-VPS entrypoint) imports this.

Returns OHLCV data in the same schema as data_feed.get_ccxt_data /
twelvedata_feed.get_twelvedata_data:
    time (pandas datetime64, UTC-naive), open, high, low, close, volume
sorted ascending, index reset to 0..n-1 -- so SignalEngine/Backtester code
doesn't need to know or care which data source it came from.
"""
import logging
from datetime import datetime, timezone

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

_connected = False


def _mt5():
    """Lazy import so this module can be imported on non-Windows hosts
    without blowing up (e.g. if smc_bot as a whole gets imported for
    testing) -- it will only fail once you actually try to use it."""
    try:
        import MetaTrader5 as mt5
    except ImportError as e:
        raise RuntimeError(
            "MetaTrader5 package not installed or not on Windows. "
            "Run `pip install MetaTrader5` on the Windows VPS running the "
            "MT5 terminal -- see MT5_VPS_SETUP.md."
        ) from e
    return mt5


def connect(login: int, password: str, server: str, terminal_path: str = "") -> None:
    """Initialize the MT5 terminal connection. Call once at startup."""
    global _connected
    mt5 = _mt5()

    kwargs = {}
    if terminal_path:
        kwargs["path"] = terminal_path

    if not mt5.initialize(**kwargs):
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")

    if not mt5.login(login, password=password, server=server):
        err = mt5.last_error()
        mt5.shutdown()
        raise RuntimeError(f"mt5.login() failed for account {login}@{server}: {err}")

    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"mt5.account_info() returned None after login: {mt5.last_error()}")

    is_demo = account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    logger.info(
        "Connected to MT5: login=%s server=%s balance=%.2f %s demo=%s",
        account.login, account.server, account.balance, account.currency, is_demo,
    )
    if not is_demo:
        logger.warning(
            "*** This MT5 account is NOT a demo account (trade_mode=%s). "
            "Refusing to place trades -- see mt5_executor.py's demo-only guard. ***",
            account.trade_mode,
        )
    _connected = True


def disconnect() -> None:
    global _connected
    if _connected:
        _mt5().shutdown()
        _connected = False


def resolve_symbol(preferred_name: str, candidates: list) -> str:
    """
    Broker demo servers often suffix/prefix symbol names differently
    (XAUUSD vs XAUUSD.m vs GOLD, BTCUSD vs BTCUSDm vs BTCUSD.). Try the
    preferred name first, then each candidate, and pick whichever one the
    broker actually has in its symbol list -- fail loudly if none match
    rather than silently trading the wrong instrument.
    """
    mt5 = _mt5()
    all_symbols = {s.name for s in mt5.symbols_get()}

    for name in [preferred_name] + list(candidates):
        if name in all_symbols:
            if not mt5.symbol_select(name, True):
                continue
            return name

    # last resort: substring match, logged loudly so you notice and can
    # hardcode the right one in config.MT5_SYMBOL_MAP instead
    base = preferred_name.replace("USD", "")
    matches = [s for s in all_symbols if base in s and "USD" in s]
    if matches:
        logger.warning(
            "No exact symbol match for %s -- guessing '%s' from %d candidates %s. "
            "Verify this is correct and set MT5_SYMBOL_MAP explicitly to silence this.",
            preferred_name, matches[0], len(matches), matches[:5],
        )
        if mt5.symbol_select(matches[0], True):
            return matches[0]

    raise RuntimeError(
        f"Could not find a broker symbol for '{preferred_name}' "
        f"(tried {[preferred_name] + list(candidates)}). "
        f"Check the exact symbol name in MT5's Market Watch and set it in "
        f"config.MT5_SYMBOL_MAP."
    )


def get_mt5_data(broker_symbol: str, timeframe: str, n_bars: int = 500) -> pd.DataFrame:
    mt5 = _mt5()
    tf_const = getattr(mt5, MT5_TIMEFRAME_MAP[timeframe])
    rates = mt5.copy_rates_from_pos(broker_symbol, tf_const, 0, n_bars)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"mt5.copy_rates_from_pos returned no data for {broker_symbol} {timeframe}: {mt5.last_error()}")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.rename(columns={"tick_volume": "volume"})
    df = df[["time", "open", "high", "low", "close", "volume"]].sort_values("time").reset_index(drop=True)
    return df


def symbol_point(broker_symbol: str) -> float:
    """MT5's smallest price increment for this symbol (used for stops-level
    checks and SL/TP normalization -- distinct from the strategy's own
    pip_size used for SL buffer math)."""
    mt5 = _mt5()
    info = mt5.symbol_info(broker_symbol)
    if info is None:
        raise RuntimeError(f"symbol_info({broker_symbol}) returned None")
    return info.point


def account_balance() -> float:
    mt5 = _mt5()
    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"account_info() returned None: {mt5.last_error()}")
    return account.balance
