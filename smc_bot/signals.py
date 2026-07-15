"""
Signal engine: combines

  1. Supply/Demand zones (zones.py)
  2. Premium/Discount position of that zone within the current dealing range
  3. HTF trend alignment (trend.py)

into a single BUY/SELL signal, replacing liquidity-sweep + BOS/CHoCH logic
entirely, and without FVG confluence (per strategy spec).

Rule set:
  BUY  -> price taps an unmitigated DEMAND zone that sits in the DISCOUNT
          half of the current dealing range, AND HTF trend is bullish.
  SELL -> price taps an unmitigated SUPPLY zone that sits in the PREMIUM
          half of the current dealing range, AND HTF trend is bearish.

  Zones in the "wrong" half of the range (e.g. a demand zone sitting in
  premium) or against HTF trend are ignored — this is what keeps the
  strategy selective rather than firing on every zone tap.
"""
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

from .indicators import atr
from .zones import detect_zones, active_zones, compute_dealing_range, Zone
from .trend import htf_trend


@dataclass
class Signal:
    symbol: str
    direction: str       # "BUY" or "SELL"
    entry: float
    stop_loss: float
    take_profits: List[float]
    zone: Zone
    dealing_range_position: str
    htf_trend: str
    bar_idx: int
    bar_time: object
    risk_reward: List[float]

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    def to_message(self) -> str:
        tps = ", ".join(f"TP{i+1}: {tp:.2f}" for i, tp in enumerate(self.take_profits))
        rr = ", ".join(f"{r:.1f}R" for r in self.risk_reward)
        arrow = "🟢 BUY" if self.direction == "BUY" else "🔴 SELL"
        return (
            f"{arrow} {self.symbol}\n"
            f"Zone: {self.zone.kind.upper()} [{self.zone.bottom:.2f} - {self.zone.top:.2f}]\n"
            f"Range position: {self.dealing_range_position.upper()} | HTF trend: {self.htf_trend.upper()}\n"
            f"Entry: {self.entry:.2f}\n"
            f"SL: {self.stop_loss:.2f}\n"
            f"{tps}\n"
            f"R multiples: {rr}\n"
            f"Time: {self.bar_time}"
        )


class SignalEngine:
    def __init__(self, symbol: str, strategy_params: dict):
        self.symbol = symbol
        self.p = strategy_params

    def _htf_idx_for(self, df_ltf: pd.DataFrame, ltf_idx: int, df_htf: pd.DataFrame) -> int:
        """
        Find the index of the last HTF bar that had already CLOSED at or
        before the LTF bar's time — this is what prevents the backtester
        from peeking at an HTF candle that hasn't finished forming yet.
        Assumes both dataframes have a 'time' column of comparable dtype.
        """
        if "time" not in df_ltf.columns or "time" not in df_htf.columns:
            # No explicit timestamps (e.g. synthetic data) — fall back to a
            # proportional mapping, which is approximate but deterministic.
            ratio = (ltf_idx + 1) / max(len(df_ltf), 1)
            return max(0, int(ratio * len(df_htf)) - 1)

        ltf_time = df_ltf["time"].iloc[ltf_idx]
        # last htf bar whose time <= ltf_time
        idx = df_htf["time"].searchsorted(ltf_time, side="right") - 1
        return max(0, min(idx, len(df_htf) - 1))

    def evaluate(
        self,
        df_ltf: pd.DataFrame,
        df_htf: pd.DataFrame,
        up_to_idx: Optional[int] = None,
        already_signaled_zone_ids: Optional[set] = None,
        precomputed: Optional[dict] = None,
    ) -> List[Signal]:
        """
        Evaluate for a new signal at bar `up_to_idx` (defaults to the last bar).
        Returns a list (usually 0 or 1) of new Signals.
        `already_signaled_zone_ids` lets the caller avoid re-alerting the
        same zone twice; pass the set of zone.formed_idx already alerted.
        """
        n = len(df_ltf)
        if up_to_idx is None:
            up_to_idx = n - 1
        if already_signaled_zone_ids is None:
            already_signaled_zone_ids = set()

        if up_to_idx < max(self.p["atr_period"], self.p["swing_left"] + self.p["swing_right"] + 2):
            return []

        zones = detect_zones(
            df_ltf,
            swing_left=self.p["swing_left"],
            swing_right=self.p["swing_right"],
            min_impulse_atr=self.p["min_impulse_atr"],
            atr_period=self.p["atr_period"],
            max_zone_age_bars=self.p["max_zone_age_bars"],
            up_to_idx=up_to_idx,
            lookback=self.p.get("zone_lookback"),
            precomputed=precomputed,
        )
        live_zones = [z for z in active_zones(zones) if (z.formed_idx, z.kind) not in already_signaled_zone_ids]
        if not live_zones:
            return []

        dealing_range = compute_dealing_range(df_ltf, self.p["dealing_range_lookback"], up_to_idx)
        htf_idx = self._htf_idx_for(df_ltf, up_to_idx, df_htf)
        trend = htf_trend(df_htf, self.p["htf_ema_fast"], self.p["htf_ema_slow"], up_to_idx=htf_idx)

        if precomputed is not None:
            local_atr = precomputed["atr"][up_to_idx]
        else:
            local_atr = atr(df_ltf, self.p["atr_period"]).iloc[up_to_idx]
        if pd.isna(local_atr):
            return []

        bar = df_ltf.iloc[up_to_idx]
        bar_time = bar["time"] if "time" in df_ltf.columns else up_to_idx

        signals: List[Signal] = []

        for z in live_zones:
            # only fire the moment price first taps into the zone this bar
            touched = bar["low"] <= z.top and bar["high"] >= z.bottom
            if not touched:
                continue

            position = dealing_range.zone_position(z)

            if z.kind == "demand" and position == "discount" and trend == "bullish":
                entry = z.top
                stop_loss = z.bottom - self.p["sl_buffer_atr"] * local_atr
                risk = entry - stop_loss
                if risk <= 0:
                    continue
                take_profits = [entry + r * risk for r in self.p["tp_r_multiples"]]
                signals.append(
                    Signal(
                        symbol=self.symbol,
                        direction="BUY",
                        entry=entry,
                        stop_loss=stop_loss,
                        take_profits=take_profits,
                        zone=z,
                        dealing_range_position=position,
                        htf_trend=trend,
                        bar_idx=up_to_idx,
                        bar_time=bar_time,
                        risk_reward=list(self.p["tp_r_multiples"]),
                    )
                )

            elif z.kind == "supply" and position == "premium" and trend == "bearish":
                entry = z.bottom
                stop_loss = z.top + self.p["sl_buffer_atr"] * local_atr
                risk = stop_loss - entry
                if risk <= 0:
                    continue
                take_profits = [entry - r * risk for r in self.p["tp_r_multiples"]]
                signals.append(
                    Signal(
                        symbol=self.symbol,
                        direction="SELL",
                        entry=entry,
                        stop_loss=stop_loss,
                        take_profits=take_profits,
                        zone=z,
                        dealing_range_position=position,
                        htf_trend=trend,
                        bar_idx=up_to_idx,
                        bar_time=bar_time,
                        risk_reward=list(self.p["tp_r_multiples"]),
                    )
                )

        return signals
