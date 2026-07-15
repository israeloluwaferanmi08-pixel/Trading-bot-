"""
TwelveData live price data with multi-key rotation.

Why this exists: ccxt only covers crypto pairs (BTCUSD via BTC/USDT), so
symbols like XAUUSD with no crypto-exchange equivalent need a real REST
data source. TwelveData's REST API works anywhere, and its free tier is
generous enough for M15/H4 polling — but each free key is rate-limited
(HTTP 429 once you exceed its per-minute/per-day credits). Running 3 keys
means when one gets rate-limited we just rotate to the next instead of
failing the whole cycle.

This is a data-source concern only — it has no effect on STRATEGY or how
signals.py decides to fire a signal.
"""
import logging
import time
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com/time_series"

INTERVAL_MAP = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1day",
}


class TwelveDataClient:
    def __init__(self, api_keys: list, cooldown_seconds: int = 60):
        if not api_keys:
            raise ValueError("TwelveDataClient needs at least one API key")
        self.api_keys = list(api_keys)
        self.cooldown_seconds = cooldown_seconds
        self._cooldown_until = {k: 0.0 for k in self.api_keys}
        self._next_idx = 0

    def _available_keys_in_order(self):
        """Round-robin starting point, but skip any key still cooling down."""
        n = len(self.api_keys)
        order = [self.api_keys[(self._next_idx + i) % n] for i in range(n)]
        self._next_idx = (self._next_idx + 1) % n
        now = time.time()
        return [k for k in order if self._cooldown_until.get(k, 0) <= now]

    def fetch(self, symbol: str, timeframe: str, n_bars: int = 500) -> pd.DataFrame:
        interval = INTERVAL_MAP.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported timeframe for TwelveData: {timeframe}")

        keys_to_try = self._available_keys_in_order()
        if not keys_to_try:
            raise RuntimeError(
                f"All {len(self.api_keys)} TwelveData keys are cooling down "
                f"(rate-limited) — will retry next cycle."
            )

        last_err = None
        for key in keys_to_try:
            try:
                resp = requests.get(
                    BASE_URL,
                    params={
                        "symbol": symbol,
                        "interval": interval,
                        "outputsize": n_bars,
                        "apikey": key,
                    },
                    timeout=15,
                )
                data = resp.json()

                if resp.status_code == 429 or (isinstance(data, dict) and data.get("code") == 429):
                    logger.warning(
                        "TwelveData key ...%s rate-limited — cooling down %ss",
                        key[-4:], self.cooldown_seconds,
                    )
                    self._cooldown_until[key] = time.time() + self.cooldown_seconds
                    last_err = RuntimeError("TwelveData rate limit (429)")
                    continue

                if isinstance(data, dict) and data.get("status") == "error":
                    raise RuntimeError(f"TwelveData error: {data.get('message', data)}")

                values = data.get("values") if isinstance(data, dict) else None
                if not values:
                    raise RuntimeError(f"TwelveData returned no values for {symbol} {timeframe}")

                df = pd.DataFrame(values)
                df["time"] = pd.to_datetime(df["datetime"])
                for col in ("open", "high", "low", "close"):
                    df[col] = df[col].astype(float)
                df["volume"] = df["volume"].astype(float) if "volume" in df.columns else 0.0
                df = df.sort_values("time").reset_index(drop=True)
                return df[["time", "open", "high", "low", "close", "volume"]]

            except requests.RequestException as e:
                last_err = e
                logger.warning("TwelveData request failed for key ...%s: %s", key[-4:], e)
                continue

        raise last_err if last_err else RuntimeError(f"TwelveData fetch failed for {symbol} {timeframe}")


_client: Optional[TwelveDataClient] = None


def get_twelvedata_data(symbol: str, timeframe: str, n_bars: int, api_keys: list, cooldown_seconds: int) -> pd.DataFrame:
    """Module-level singleton so cooldown state survives across calls within
    the same process (fresh again on restart, which is fine — a rate limit
    cooldown is only ever ~60s anyway)."""
    global _client
    if _client is None:
        _client = TwelveDataClient(api_keys, cooldown_seconds)
    return _client.fetch(symbol, timeframe, n_bars)
