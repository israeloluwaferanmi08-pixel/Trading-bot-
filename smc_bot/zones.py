"""
Supply / Demand zone detection + Premium / Discount (dealing range) logic.

Zone detection method (documented since it's a judgment call):

1. Find confirmed swing highs / swing lows (fractals) on the LTF.
2. A "leg" is the price move from one confirmed swing point to the next.
3. A leg counts as an IMPULSE (i.e. it "leaves a zone behind") if it travels
   at least `min_impulse_atr` * ATR from the origin swing to the resulting
   swing. This is meant to approximate a strong, displaced move away from
   a base, which is what leaves supply/demand imbalance behind.
4. The ZONE is the origin candle (the last opposing candle before the
   impulsive leg started):
     - Demand zone = the last down/base candle before an impulsive rally.
       Zone box = [min(open, close), low] of that candle (the "base").
     - Supply zone = the last up/base candle before an impulsive drop.
       Zone box = [high, max(open, close)] of that candle.
   Using the single origin candle (rather than the whole base) keeps zones
   tight and closer to how the imbalance is actually defined.
5. A zone is MITIGATED (and removed from consideration) the first time price
   closes back through the zone's far edge after formation.
6. A zone STALES OUT (dropped) if it goes untouched for more than
   `max_zone_age_bars`.

Premium / Discount:
   - Dealing range = highest high & lowest low over the last
     `dealing_range_lookback` bars.
   - Equilibrium = midpoint of that range.
   - Discount = price/zone below equilibrium (favors longs).
   - Premium = price/zone above equilibrium (favors shorts).
"""
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

from .indicators import atr, swing_points


@dataclass
class Zone:
    kind: str          # "demand" or "supply"
    top: float
    bottom: float
    formed_idx: int     # integer position in the dataframe where the zone candle sits
    formed_time: pd.Timestamp
    confirmed_idx: int   # index at which the zone became known (look-ahead safe point)
    mitigated: bool = False
    mitigated_idx: Optional[int] = None
    strength: float = 0.0   # 0-1 score, bigger impulse / tighter base = stronger

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return self.top - self.bottom


def precompute_indicators(df: pd.DataFrame, swing_left: int = 2, swing_right: int = 2, atr_period: int = 14) -> dict:
    """
    Compute ATR and swing-point arrays ONCE for the full dataframe.

    These are expensive to recompute (pandas rolling mean, numpy sliding
    windows) and don't change for bars that were already visible, so a
    walk-forward backtest should compute them a single time up front and
    hand the resulting arrays into detect_zones() on every bar via the
    `precomputed` argument, instead of recomputing on a growing window
    every single bar.
    """
    a_vals = atr(df, atr_period).values
    is_high_s, is_low_s = swing_points(df, swing_left, swing_right)
    return {
        "atr": a_vals,
        "is_high": is_high_s.values,
        "is_low": is_low_s.values,
        "open": df["open"].values,
        "high": df["high"].values,
        "low": df["low"].values,
        "close": df["close"].values,
        "time": df["time"].values if "time" in df.columns else None,
    }


def detect_zones(
    df: pd.DataFrame,
    swing_left: int = 2,
    swing_right: int = 2,
    min_impulse_atr: float = 1.5,
    atr_period: int = 14,
    max_zone_age_bars: int = 250,
    up_to_idx: Optional[int] = None,
    lookback: Optional[int] = None,
    precomputed: Optional[dict] = None,
) -> List[Zone]:
    """
    Detect supply/demand zones on df (must have open/high/low/close, ascending
    time index reset to 0..n-1).

    `up_to_idx`: if given, only bars [0, up_to_idx] are considered "visible"
    (used by the backtester to guarantee no look-ahead). Defaults to the
    whole dataframe (for live use, where df is already just "up to now").

    `lookback`: if given, only scan the last `lookback` bars for zones
    instead of the entire history — this bounds the per-call cost so a
    walk-forward backtest over thousands of bars stays fast.

    `precomputed`: optional dict from precompute_indicators(df, ...) for the
    FULL dataframe. Strongly recommended for backtesting — avoids
    recomputing ATR/swing-points on every single bar. If omitted, indicators
    are computed on the fly for just the scan window (fine for live/one-off use).
    """
    n = len(df)
    if up_to_idx is None:
        up_to_idx = n - 1
    up_to_idx = min(up_to_idx, n - 1)

    scan_start = 0
    if lookback is not None:
        scan_start = max(0, up_to_idx - lookback + 1)
    # give ATR a warmup buffer before scan_start so its early values aren't NaN
    atr_start = max(0, scan_start - atr_period)
    offset = atr_start  # to translate sub-frame positions back to df positions

    if precomputed is not None:
        a_vals = precomputed["atr"][atr_start: up_to_idx + 1]
        is_high = precomputed["is_high"][atr_start: up_to_idx + 1]
        is_low = precomputed["is_low"][atr_start: up_to_idx + 1]
        open_v = precomputed["open"][atr_start: up_to_idx + 1]
        high_v = precomputed["high"][atr_start: up_to_idx + 1]
        low_v = precomputed["low"][atr_start: up_to_idx + 1]
        close_v = precomputed["close"][atr_start: up_to_idx + 1]
        time_v = precomputed["time"][atr_start: up_to_idx + 1] if precomputed["time"] is not None else None
    else:
        sub = df.iloc[atr_start: up_to_idx + 1].reset_index(drop=True)
        a_vals = atr(sub, atr_period).values
        is_high_s, is_low_s = swing_points(sub, swing_left, swing_right)
        is_high, is_low = is_high_s.values, is_low_s.values
        open_v = sub["open"].values
        high_v = sub["high"].values
        low_v = sub["low"].values
        close_v = sub["close"].values
        time_v = sub["time"].values if "time" in sub.columns else None

    sub_n = len(open_v)
    scan_start_local = scan_start - offset

    # Collect confirmed swing points that are knowable by up_to_idx and fall
    # within the scan window.
    swings = []  # (position [local to sub], kind, price, confirmed_at [local])
    for i in range(max(swing_left, scan_start_local), sub_n - swing_right):
        confirmed_at = i + swing_right
        if is_high[i]:
            swings.append((i, "high", high_v[i], confirmed_at))
        if is_low[i]:
            swings.append((i, "low", low_v[i], confirmed_at))

    swings.sort(key=lambda s: s[0])
    up_to_idx = sub_n - 1

    zones: List[Zone] = []

    for j in range(1, len(swings)):
        prev_pos, prev_kind, prev_price, _ = swings[j - 1]
        cur_pos, cur_kind, cur_price, cur_confirmed = swings[j]

        if prev_kind == cur_kind:
            continue  # need alternating low->high (rally) or high->low (drop)

        leg_range = abs(cur_price - prev_price)
        local_atr = a_vals[cur_pos]
        if pd.isna(local_atr) or local_atr <= 0:
            continue
        if leg_range < min_impulse_atr * local_atr:
            continue  # not a strong enough displacement

        origin = prev_pos
        o, h, l, c = open_v[origin], high_v[origin], low_v[origin], close_v[origin]
        origin_time = time_v[origin] if time_v is not None else origin

        if prev_kind == "low" and cur_kind == "high":
            # impulsive rally from prev_pos (swing low) to cur_pos (swing high)
            # demand zone = last down/base candle just before the rally leg
            top = max(o, c)
            bottom = l
            zones.append(Zone(
                kind="demand",
                top=top,
                bottom=bottom,
                formed_idx=origin,
                formed_time=origin_time,
                confirmed_idx=cur_confirmed,
                strength=min(1.0, leg_range / (local_atr * min_impulse_atr * 3)),
            ))

        elif prev_kind == "high" and cur_kind == "low":
            bottom = min(o, c)
            top = h
            zones.append(Zone(
                kind="supply",
                top=top,
                bottom=bottom,
                formed_idx=origin,
                formed_time=origin_time,
                confirmed_idx=cur_confirmed,
                strength=min(1.0, leg_range / (local_atr * min_impulse_atr * 3)),
            ))

    # Mark mitigation: scan forward from each zone's confirmed_idx to up_to_idx.
    close_vals = close_v
    for z in zones:
        start = z.confirmed_idx + 1
        if start > up_to_idx:
            hit_pos = -1
        else:
            window = close_vals[start: up_to_idx + 1]
            if z.kind == "demand":
                mask = window < z.bottom
            else:
                mask = window > z.top
            hit_pos = int(mask.argmax()) if mask.any() else -1
        if hit_pos >= 0:
            z.mitigated = True
            z.mitigated_idx = start + hit_pos
        # stale check (only if never mitigated)
        elif (up_to_idx - z.confirmed_idx) > max_zone_age_bars:
            z.mitigated = True  # treat stale zones as no longer tradable
            z.mitigated_idx = None

    # Translate indices from the (possibly windowed) sub-frame back to the
    # caller's global index space, so formed_idx stays a stable identity for
    # a given zone across calls with different scan windows (important for
    # de-duplicating alerts / backtester bookkeeping).
    for z in zones:
        z.formed_idx += offset
        z.confirmed_idx += offset
        if z.mitigated_idx is not None:
            z.mitigated_idx += offset

    return zones


def active_zones(zones: List[Zone]) -> List[Zone]:
    return [z for z in zones if not z.mitigated]


@dataclass
class DealingRange:
    high: float
    low: float

    @property
    def equilibrium(self) -> float:
        return (self.high + self.low) / 2.0

    def position_of(self, price: float) -> str:
        """Return 'premium', 'discount', or 'equilibrium' for a given price."""
        eq = self.equilibrium
        if price > eq:
            return "premium"
        elif price < eq:
            return "discount"
        return "equilibrium"

    def zone_position(self, zone: Zone) -> str:
        return self.position_of(zone.mid)


def compute_dealing_range(df: pd.DataFrame, lookback: int = 100, up_to_idx: Optional[int] = None) -> DealingRange:
    n = len(df)
    if up_to_idx is None:
        up_to_idx = n - 1
    start = max(0, up_to_idx - lookback + 1)
    window = df.iloc[start: up_to_idx + 1]
    return DealingRange(high=window["high"].max(), low=window["low"].min())
