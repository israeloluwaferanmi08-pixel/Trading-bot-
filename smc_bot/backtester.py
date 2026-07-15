"""
Walk-forward, look-ahead-safe backtester.

At every LTF bar i, the SignalEngine only ever sees df_ltf[:i+1] and the
HTF bars that had already closed by that time (see SignalEngine._htf_idx_for).
This means zones, dealing ranges and trend are recomputed as of that exact
point in history — no future information leaks into a signal.

Trade management model (kept simple and transparent):
  - One trade open at a time per symbol (configurable via max_open_trades).
  - Position is expressed in R (risk units): risk_amount = balance * risk_percent/100.
  - Multiple take-profits split the position evenly; when TP1 is hit the
    remaining position's stop is moved to breakeven (a common, conservative
    way to lock in the S/D-zone setup once it starts working).
  - If SL and a TP both fall inside the same bar's range, SL is assumed to
    execute first (worst case / conservative assumption) unless the bar's
    open price is already beyond the TP in the trade's favor.
  - Spread + slippage are modelled as a fixed price cost applied against the
    entry fill.
"""
from dataclasses import dataclass, field
from typing import List, Optional
import pandas as pd

from .signals import SignalEngine, Signal
from .zones import precompute_indicators


@dataclass
class Trade:
    symbol: str
    direction: str
    entry: float
    stop_loss: float
    take_profits: List[float]
    open_idx: int
    open_time: object
    remaining_fraction: float = 1.0
    realized_r: float = 0.0
    tp_hits: List[bool] = field(default_factory=list)
    closed: bool = False
    close_idx: Optional[int] = None
    close_time: Optional[object] = None
    initial_risk: float = 0.0

    def __post_init__(self):
        if not self.tp_hits:
            self.tp_hits = [False] * len(self.take_profits)
        self.initial_risk = abs(self.entry - self.stop_loss)


class Backtester:
    def __init__(self, symbol: str, strategy_params: dict, backtest_params: dict, pip_size: float = 0.01):
        self.symbol = symbol
        self.strategy_params = strategy_params
        self.bp = backtest_params
        self.pip_size = pip_size
        self.engine = SignalEngine(symbol, strategy_params)

    def run(self, df_ltf: pd.DataFrame, df_htf: pd.DataFrame, max_open_trades: int = 1, recalc_every: int = 1):
        balance = self.bp["initial_balance"]
        risk_pct = self.bp["risk_percent"] / 100.0
        cost = (self.bp.get("spread_pips", 0) + self.bp.get("slippage_pips", 0)) * self.pip_size

        trades: List[Trade] = []
        open_trades: List[Trade] = []
        signaled_zone_ids = set()

        # Loss-cluster cooldown: recent (bar_idx, entry_price, atr_at_close) for
        # losing trades. A new signal is skipped if its entry sits within
        # `cooldown_atr_mult` ATR of a loss that closed within `cooldown_bars`.
        # See config.py STRATEGY docstring for why this exists.
        cooldown_atr_mult = self.strategy_params.get("loss_cooldown_atr_mult") or 0
        cooldown_bars = self.strategy_params.get("loss_cooldown_bars") or 0
        recent_losses: List[tuple] = []

        # Consecutive-loss (streak) cooldown: a DIFFERENT mechanism from the
        # one above — this one doesn't care about price at all, only about
        # how many losses in a row just happened. If `consecutive_loss_limit`
        # losing trades close back-to-back (any wins in between reset the
        # streak), ALL new signals are blocked for `consecutive_loss_cooldown_bars`
        # bars, regardless of price/direction. This is a blunt circuit-breaker
        # for "the strategy/regime looks off right now", as opposed to the
        # ATR cooldown's "don't re-enter this exact spot". Set
        # consecutive_loss_limit to 0/None to disable.
        consecutive_loss_limit = self.strategy_params.get("consecutive_loss_limit") or 0
        consecutive_loss_cooldown_bars = self.strategy_params.get("consecutive_loss_cooldown_bars") or 0
        consecutive_losses = 0
        streak_cooldown_until = -1  # bar idx before which new signals are blocked

        n = len(df_ltf)
        warmup = max(self.strategy_params["atr_period"],
                     self.strategy_params["swing_left"] + self.strategy_params["swing_right"] + 2,
                     self.strategy_params["dealing_range_lookback"] // 4)

        precomputed = precompute_indicators(
            df_ltf,
            swing_left=self.strategy_params["swing_left"],
            swing_right=self.strategy_params["swing_right"],
            atr_period=self.strategy_params["atr_period"],
        )

        for i in range(warmup, n):
            bar = df_ltf.iloc[i]

            # ---- 1. manage open trades on this bar (fills tr.realized_r / tr.closed) ----
            still_open = []
            for tr in open_trades:
                self._update_trade(tr, bar)
                if not tr.closed:
                    still_open.append(tr)
                else:
                    if tr.realized_r <= 0:
                        if cooldown_atr_mult and cooldown_bars:
                            atr_at_close = precomputed["atr"][i]
                            if pd.isna(atr_at_close):
                                atr_at_close = precomputed["atr"][tr.open_idx]
                            recent_losses.append((i, tr.entry, atr_at_close))
                        if consecutive_loss_limit:
                            consecutive_losses += 1
                            if consecutive_losses >= consecutive_loss_limit:
                                streak_cooldown_until = i + consecutive_loss_cooldown_bars
                                consecutive_losses = 0  # needs a fresh streak to re-trigger
                    elif consecutive_loss_limit:
                        consecutive_losses = 0  # any win resets the streak
            open_trades = still_open

            if cooldown_bars:
                recent_losses = [(bi, px, a) for (bi, px, a) in recent_losses if i - bi <= cooldown_bars]

            # ---- 2. look for a new signal ----
            in_streak_cooldown = consecutive_loss_limit and i < streak_cooldown_until
            if (
                len(open_trades) < max_open_trades
                and not in_streak_cooldown
                and (recalc_every == 1 or i % recalc_every == 0)
            ):
                new_signals = self.engine.evaluate(
                    df_ltf, df_htf, up_to_idx=i, already_signaled_zone_ids=signaled_zone_ids,
                    precomputed=precomputed,
                )
                for sig in new_signals:
                    if len(open_trades) >= max_open_trades:
                        break
                    # mark as signaled regardless of cooldown so we don't keep
                    # re-evaluating the same zone every bar
                    signaled_zone_ids.add((sig.zone.formed_idx, sig.zone.kind))

                    if cooldown_atr_mult and recent_losses:
                        blocked = any(
                            abs(sig.entry - px) <= cooldown_atr_mult * a
                            for (_, px, a) in recent_losses if a and not pd.isna(a)
                        )
                        if blocked:
                            continue

                    entry = sig.entry + (cost if sig.direction == "BUY" else -cost)
                    tr = Trade(
                        symbol=self.symbol,
                        direction=sig.direction,
                        entry=entry,
                        stop_loss=sig.stop_loss,
                        take_profits=sig.take_profits,
                        open_idx=i,
                        open_time=bar.get("time", i),
                    )
                    open_trades.append(tr)
                    trades.append(tr)

        # close any still-open trades at the last available price (mark-to-market)
        for tr in open_trades:
            last_close = df_ltf["close"].iloc[-1]
            self._force_close(tr, last_close, n - 1, df_ltf["time"].iloc[-1] if "time" in df_ltf.columns else n - 1)

        # ---- proper, race-free balance accounting ----
        balance = self.bp["initial_balance"]
        equity_curve = []
        running_bar_i = warmup
        trades_sorted = sorted(trades, key=lambda t: t.close_idx if t.close_idx is not None else t.open_idx)
        for tr in trades_sorted:
            risk_amount = balance * risk_pct
            balance += tr.realized_r * risk_amount
            equity_curve.append({
                "idx": tr.close_idx,
                "time": tr.close_time,
                "balance": balance,
                "trade_r": tr.realized_r,
                "symbol": tr.symbol,
                "direction": tr.direction,
            })

        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "final_balance": balance,
            "initial_balance": self.bp["initial_balance"],
        }

    def _update_trade(self, tr: Trade, bar) -> None:
        low, high = bar["low"], bar["high"]

        if tr.direction == "BUY":
            hit_sl = low <= tr.stop_loss
            for k, tp in enumerate(tr.take_profits):
                if not tr.tp_hits[k] and high >= tp:
                    self._partial_close(tr, k, tp, bar)
            if hit_sl and not tr.closed:
                self._stop_out(tr, bar)
        else:  # SELL
            hit_sl = high >= tr.stop_loss
            for k, tp in enumerate(tr.take_profits):
                if not tr.tp_hits[k] and low <= tp:
                    self._partial_close(tr, k, tp, bar)
            if hit_sl and not tr.closed:
                self._stop_out(tr, bar)

    def _partial_close(self, tr: Trade, tp_index: int, tp_price: float, bar) -> None:
        tr.tp_hits[tp_index] = True
        n_tps = len(tr.take_profits)
        fraction = 1.0 / n_tps
        r_this_leg = (abs(tp_price - tr.entry) / tr.initial_risk) * fraction
        tr.realized_r += r_this_leg
        tr.remaining_fraction -= fraction

        # move stop to breakeven after first TP (simple, conservative trade mgmt)
        if tp_index == 0:
            tr.stop_loss = tr.entry

        if tr.remaining_fraction <= 1e-9:
            tr.closed = True
            tr.close_idx = bar.name if hasattr(bar, "name") else None
            tr.close_time = bar.get("time", tr.close_idx)

    def _stop_out(self, tr: Trade, bar) -> None:
        # remaining fraction is stopped out at tr.stop_loss (breakeven if TP1 already hit, else full -1R)
        r_this_leg = ((tr.stop_loss - tr.entry) / tr.initial_risk if tr.direction == "BUY"
                      else (tr.entry - tr.stop_loss) / tr.initial_risk) * tr.remaining_fraction
        tr.realized_r += r_this_leg
        tr.remaining_fraction = 0.0
        tr.closed = True
        tr.close_idx = bar.name if hasattr(bar, "name") else None
        tr.close_time = bar.get("time", tr.close_idx)

    def _force_close(self, tr: Trade, price: float, idx: int, time_val) -> None:
        r_this_leg = ((price - tr.entry) / tr.initial_risk if tr.direction == "BUY"
                      else (tr.entry - price) / tr.initial_risk) * tr.remaining_fraction
        tr.realized_r += r_this_leg
        tr.remaining_fraction = 0.0
        tr.closed = True
        tr.close_idx = idx
        tr.close_time = time_val
