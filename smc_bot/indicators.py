"""
Low-level, dependency-light indicator helpers used by the zone / trend logic.
All functions take/return pandas objects and are look-ahead safe
(each row's value only uses data available up to that row, EXCEPT the
fractal swing detector which by definition needs `right` future bars to
confirm a swing — this is clearly flagged wherever it's used so the
backtester never uses a swing point before it could actually be confirmed).
"""
import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def swing_points(df: pd.DataFrame, left: int = 2, right: int = 2):
    """
    Fractal-style swing high/low detector.

    A bar `i` is a swing high if its high is the (unique) max of the window
    [i-left, i+right], and a swing low if its low is the (unique) min of
    that window.

    Returns two boolean Series: (is_swing_high, is_swing_low).

    IMPORTANT (look-ahead): a swing at index i is only *confirmed* once bar
    i+right has closed. Callers doing walk-forward / backtesting must only
    treat the swing as "known" starting at index i+right, not at index i.

    Vectorized with numpy's sliding_window_view — this runs inside the hot
    loop of a walk-forward backtest (recomputed on a rolling window every
    bar), so a plain per-bar Python loop is too slow at scale.
    """
    from numpy.lib.stride_tricks import sliding_window_view

    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    n = len(df)
    window_len = left + right + 1

    is_high = np.zeros(n, dtype=bool)
    is_low = np.zeros(n, dtype=bool)

    if n >= window_len:
        win_h = sliding_window_view(h, window_len)   # shape (n-window_len+1, window_len)
        win_l = sliding_window_view(l, window_len)
        center_h = h[left: n - right]
        center_l = l[left: n - right]

        max_h = win_h.max(axis=1)
        min_l = win_l.min(axis=1)
        count_max_h = (win_h == max_h[:, None]).sum(axis=1)
        count_min_l = (win_l == min_l[:, None]).sum(axis=1)

        is_high[left: n - right] = (center_h == max_h) & (count_max_h == 1)
        is_low[left: n - right] = (center_l == min_l) & (count_min_l == 1)

    return pd.Series(is_high, index=df.index), pd.Series(is_low, index=df.index)


def body_size(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs()


def candle_range(df: pd.DataFrame) -> pd.Series:
    return df["high"] - df["low"]
