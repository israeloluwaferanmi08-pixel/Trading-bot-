"""
Break of Structure (BOS) and Change of Character (CHoCH) detection.

Definitions used here (standard SMC usage):

  - We track a running "structure trend" state: bullish, bearish, or
    undefined (before the first break has happened).
  - We track the most recently CONFIRMED swing high and swing low —
    "confirmed" meaning `swing_right` bars have already closed past the
    fractal, same look-ahead-safe rule used by swing_points() in
    indicators.py and by zones.py.
  - BOS  = price CLOSES beyond a swing point in the direction that AGREES
    with the current trend -> trend continuation.
        trend bullish/undefined + close > last swing high -> bullish BOS
        trend bearish/undefined + close < last swing low  -> bearish BOS
  - CHoCH = price CLOSES beyond a swing point in the direction that
    DISAGREES with the current trend -> the trend flips.
        trend bullish + close < last swing low  -> bearish CHoCH
        trend bearish + close > last swing high -> bullish CHoCH
  - Before any break has occurred, trend is "undefined"; the very first
    break of either side simply establishes the initial trend (labeled a
    BOS by convention, since there's no prior trend to "change").

  Each swing level is only used once: after it's broken it's marked
  "used" and won't fire again. Whenever a NEW swing high/low is confirmed
  it replaces the previous one of that kind as the active reference,
  whether or not the old one was ever broken — this keeps the reference
  current instead of chasing a stale, deeply-buried level.

Look-ahead safety: exactly mirrors zones.py — a swing at fractal index i
is only usable starting at i + swing_right, and a break is only knowable
at the close of the breaking bar, so structure state as of bar
`up_to_idx` only ever uses bars <= up_to_idx.
"""
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

from .indicators import swing_points


@dataclass
class StructureEvent:
    idx: int            # bar index where the break/close confirmed the event
    kind: str            # "BOS" or "CHoCH"
    direction: str        # "bullish" or "bearish"
    level: float          # the swing price that was broken
    swing_idx: int        # bar index of the swing point that was broken


def compute_structure(
    df: pd.DataFrame,
    swing_left: int = 2,
    swing_right: int = 2,
    up_to_idx: Optional[int] = None,
    lookback: Optional[int] = None,
    precomputed: Optional[dict] = None,
) -> List[StructureEvent]:
    """
    Detect BOS/CHoCH events on df (must have open/high/low/close, ascending
    time index reset to 0..n-1).

    `up_to_idx`: only bars [0, up_to_idx] are considered "visible" (used by
    the backtester to guarantee no look-ahead). Defaults to the whole df.

    `lookback`: if given, only scan the last `lookback` bars instead of the
    entire history, mirroring detect_zones()'s windowing so a walk-forward
    backtest stays fast. Trend state is then only as good as what's visible
    in that window (same tradeoff zones.py already makes).

    `precomputed`: optional dict from zones.precompute_indicators(df, ...)
    for the FULL dataframe (must have been computed with the same
    swing_left/swing_right). Reuses its is_high/is_low/high/low/close
    arrays instead of recomputing swing points. If omitted, swings are
    computed on the fly for just the scan window.
    """
    n = len(df)
    if up_to_idx is None:
        up_to_idx = n - 1
    up_to_idx = min(up_to_idx, n - 1)

    scan_start = 0
    if lookback is not None:
        scan_start = max(0, up_to_idx - lookback + 1)
    offset = scan_start

    if precomputed is not None:
        is_high = precomputed["is_high"][scan_start: up_to_idx + 1]
        is_low = precomputed["is_low"][scan_start: up_to_idx + 1]
        high_v = precomputed["high"][scan_start: up_to_idx + 1]
        low_v = precomputed["low"][scan_start: up_to_idx + 1]
        close_v = precomputed["close"][scan_start: up_to_idx + 1]
    else:
        sub = df.iloc[scan_start: up_to_idx + 1].reset_index(drop=True)
        is_high_s, is_low_s = swing_points(sub, swing_left, swing_right)
        is_high, is_low = is_high_s.values, is_low_s.values
        high_v = sub["high"].values
        low_v = sub["low"].values
        close_v = sub["close"].values

    sub_n = len(close_v)

    # Confirmed swings, sorted by the bar at which they become knowable.
    swings = []  # (confirmed_at, kind, price, pos [local to sub])
    for i in range(swing_left, sub_n - swing_right):
        confirmed_at = i + swing_right
        if is_high[i]:
            swings.append((confirmed_at, "high", high_v[i], i))
        if is_low[i]:
            swings.append((confirmed_at, "low", low_v[i], i))
    swings.sort(key=lambda s: s[0])

    ptr = 0
    n_swings = len(swings)

    trend = "undefined"
    last_high = None  # dict(price, pos, used)
    last_low = None
    events: List[StructureEvent] = []

    for i in range(sub_n):
        while ptr < n_swings and swings[ptr][0] == i:
            _, kind, price, pos = swings[ptr]
            if kind == "high":
                last_high = {"price": price, "pos": pos, "used": False}
            else:
                last_low = {"price": price, "pos": pos, "used": False}
            ptr += 1

        close = close_v[i]

        if last_high is not None and not last_high["used"] and close > last_high["price"]:
            kind = "BOS" if trend in ("bullish", "undefined") else "CHoCH"
            events.append(StructureEvent(
                idx=i, kind=kind, direction="bullish",
                level=last_high["price"], swing_idx=last_high["pos"],
            ))
            trend = "bullish"
            last_high["used"] = True

        if last_low is not None and not last_low["used"] and close < last_low["price"]:
            kind = "BOS" if trend in ("bearish", "undefined") else "CHoCH"
            events.append(StructureEvent(
                idx=i, kind=kind, direction="bearish",
                level=last_low["price"], swing_idx=last_low["pos"],
            ))
            trend = "bearish"
            last_low["used"] = True

    # Translate indices from the (possibly windowed) sub-frame back to the
    # caller's global index space, same as detect_zones() does.
    for e in events:
        e.idx += offset
        e.swing_idx += offset

    return events


def structure_state(events: List[StructureEvent]) -> str:
    """Current trend implied by the most recent event ('undefined' if none yet)."""
    if not events:
        return "undefined"
    return events[-1].direction


def last_choch(events: List[StructureEvent]) -> Optional[StructureEvent]:
    """Most recent CHoCH event, if any — useful for flagging a fresh reversal."""
    for e in reversed(events):
        if e.kind == "CHoCH":
            return e
    return None
