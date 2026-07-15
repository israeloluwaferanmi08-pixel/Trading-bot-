"""
Higher-timeframe trend filter.

Method: EMA(fast) vs EMA(slow) on the HTF, plus price position relative to
both. This is deliberately simple and robust rather than another layer of
structure-break logic (since BOS/CHoCH was explicitly dropped per the
strategy spec) — it just answers "is the higher timeframe currently
trending up, down, or flat/undecided".

    bullish: close > ema_fast > ema_slow
    bearish: close < ema_fast < ema_slow
    neutral: anything else (e.g. EMAs crossed/flat, price chopping between them)
"""
from typing import Optional
import pandas as pd

from .indicators import ema


def htf_trend(df_htf: pd.DataFrame, fast: int = 50, slow: int = 200, up_to_idx: Optional[int] = None) -> str:
    n = len(df_htf)
    if up_to_idx is None:
        up_to_idx = n - 1
    up_to_idx = min(up_to_idx, n - 1)

    if up_to_idx < slow:
        return "neutral"  # not enough data yet to trust the slow EMA

    close = df_htf["close"].iloc[: up_to_idx + 1]
    ema_fast = ema(close, fast).iloc[-1]
    ema_slow = ema(close, slow).iloc[-1]
    last_close = close.iloc[-1]

    if last_close > ema_fast > ema_slow:
        return "bullish"
    if last_close < ema_fast < ema_slow:
        return "bearish"
    return "neutral"
