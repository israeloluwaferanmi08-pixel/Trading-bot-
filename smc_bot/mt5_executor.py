"""
Places and manages MT5 orders for signals from the same SignalEngine used
by the backtester -- this is the live analog of backtester.py's
_update_trade / _partial_close / _stop_out, translated into real MT5
position management calls.

Trade management model (mirrors backtester.py as closely as MT5 allows):
  - Position size from risk_percent of current account balance / SL distance.
  - Only 2 take-profits are supported for live partial closes (TP1, TP2) --
    matches config.STRATEGY["tp_r_multiples"] default of (2.0, 3.0). If you
    change tp_r_multiples to more than 2 values, only the first two are
    used for live partials; extra values are ignored with a warning.
  - TP1 hit -> close half the position, move remaining SL to breakeven,
    start trailing.
  - Trailing (once active): SL follows the most recently confirmed swing
    low (BUY) / swing high (SELL) from the LTF data already being fetched
    each poll cycle, offset by the same sl_buffer_atr used on the initial
    stop. Only ever tightens. This is a poll-interval-resolution analog of
    the backtester's bar-close trailing -- it can't be perfectly identical
    to a bar-by-bar backtest since it only re-evaluates once per poll.
  - SAFETY: refuses to place any order unless the connected account's
    trade_mode is ACCOUNT_TRADE_MODE_DEMO. This is intentional and should
    not be bypassed without fully understanding the risk -- see README.

Every open MT5 position opened by this bot carries `magic` = config
MT5_MAGIC_NUMBER, and a comment encoding the internal signal_id, so it can
be told apart from any manual trades on the same account and reconciled
against the SQLite Store like Telegram-only signals already are.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

import pandas as pd

from .zones import precompute_indicators

logger = logging.getLogger(__name__)


def _mt5():
    import MetaTrader5 as mt5
    return mt5


@dataclass
class ManagedPosition:
    signal_id: int
    symbol: str
    broker_symbol: str
    direction: str
    entry: float
    initial_sl: float
    take_profits: list
    volume: float
    ticket_tp1: Optional[int] = None   # ticket for the TP1-sized half
    ticket_tp2: Optional[int] = None   # ticket for the TP2-sized half
    tp1_hit: bool = False
    trail_active: bool = False
    strategy_params: dict = field(default_factory=dict)


class MT5Executor:
    def __init__(self, magic_number: int, demo_only: bool = True):
        self.magic_number = magic_number
        self.demo_only = demo_only
        self.positions: Dict[int, ManagedPosition] = {}  # signal_id -> ManagedPosition

    def _assert_demo(self) -> None:
        mt5 = _mt5()
        account = mt5.account_info()
        if account is None:
            raise RuntimeError(f"account_info() failed: {mt5.last_error()}")
        if self.demo_only and account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise RuntimeError(
                "Refusing to trade: connected MT5 account is not a demo account "
                "(trade_mode != ACCOUNT_TRADE_MODE_DEMO). Set demo_only=False in "
                "MT5Executor explicitly if you really intend to trade a live account "
                "-- this guard exists on purpose, don't remove it casually."
            )

    def _normalize_volume(self, broker_symbol: str, raw_volume: float) -> float:
        mt5 = _mt5()
        info = mt5.symbol_info(broker_symbol)
        step = info.volume_step or 0.01
        vol = max(info.volume_min, min(info.volume_max, round(raw_volume / step) * step))
        return round(vol, 8)

    def compute_volume(self, broker_symbol: str, risk_amount: float, sl_distance_price: float) -> float:
        """
        risk_amount: account currency amount to risk on this trade
                     (balance * risk_percent/100, computed by the caller).
        sl_distance_price: |entry - stop_loss| in price terms.
        Uses the symbol's tick_value/tick_size to convert a price-distance
        risk into a lot size, same math MT5's own position-size calculators
        use. Falls back to a conservative 0.01 lots if symbol info is
        incomplete rather than guessing large.
        """
        mt5 = _mt5()
        info = mt5.symbol_info(broker_symbol)
        if info is None or not info.trade_tick_value or not info.trade_tick_size:
            logger.warning("Incomplete symbol_info for %s -- defaulting to 0.01 lots.", broker_symbol)
            return 0.01

        ticks = sl_distance_price / info.trade_tick_size
        loss_per_lot = ticks * info.trade_tick_value
        if loss_per_lot <= 0:
            return info.volume_min
        raw_volume = risk_amount / loss_per_lot
        return self._normalize_volume(broker_symbol, raw_volume)

    def open_trade(self, signal_id: int, sig, broker_symbol: str, risk_amount: float, strategy_params: dict) -> Optional[ManagedPosition]:
        """
        Splits the position into two MT5 orders up front (one sized for
        TP1, one for TP2) since MT5 doesn't natively support "close half
        the position at price X" -- this is the standard way to do partial
        take-profits in MT5. Both share the same SL initially.
        """
        self._assert_demo()
        mt5 = _mt5()

        if len(sig.take_profits) < 2:
            logger.warning("Signal has < 2 take profits; live partials need exactly 2. Using TP1 for both legs.")
            tps = [sig.take_profits[0], sig.take_profits[0]]
        else:
            tps = sig.take_profits[:2]

        total_volume = self.compute_volume(broker_symbol, risk_amount, sig.risk_distance)
        half = self._normalize_volume(broker_symbol, total_volume / 2)
        if half <= 0:
            logger.error("Computed zero volume for signal #%s on %s -- skipping order.", signal_id, sig.symbol)
            return None

        order_type = mt5.ORDER_TYPE_BUY if sig.direction == "BUY" else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(broker_symbol)
        price = tick.ask if sig.direction == "BUY" else tick.bid

        pos = ManagedPosition(
            signal_id=signal_id, symbol=sig.symbol, broker_symbol=broker_symbol,
            direction=sig.direction, entry=price, initial_sl=sig.stop_loss,
            take_profits=tps, volume=half * 2, strategy_params=strategy_params,
        )

        failed_legs = []
        for leg_name, tp in (("tp1", tps[0]), ("tp2", tps[1])):
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": broker_symbol,
                "volume": half,
                "type": order_type,
                "price": price,
                "sl": sig.stop_loss,
                "tp": tp,
                "deviation": 20,
                "magic": self.magic_number,
                "comment": f"smc_bot#{signal_id}:{leg_name}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(
                    "order_send failed for signal #%s (%s leg): retcode=%s comment=%s",
                    signal_id, leg_name, getattr(result, "retcode", None), getattr(result, "comment", None),
                )
                failed_legs.append(leg_name)
                continue
            ticket = result.order
            if leg_name == "tp1":
                pos.ticket_tp1 = ticket
            else:
                pos.ticket_tp2 = ticket

        # Partial failure: one leg opened, the other didn't. A lone half
        # position doesn't match the risk size this trade was supposed to
        # carry, and the rest of MT5Executor (manage_positions, trailing,
        # breakeven-on-TP1) assumes both legs exist. Rather than leave that
        # orphan running unmanaged, close whichever leg DID open and treat
        # the whole signal as not-entered.
        if failed_legs and (pos.ticket_tp1 is not None or pos.ticket_tp2 is not None):
            opened_ticket = pos.ticket_tp1 if pos.ticket_tp1 is not None else pos.ticket_tp2
            opened_leg = "tp1" if pos.ticket_tp1 is not None else "tp2"
            logger.error(
                "Signal #%s: %s leg failed but %s already opened (ticket=%s) -- "
                "closing it to avoid an orphaned half-position.",
                signal_id, failed_legs, opened_leg, opened_ticket,
            )
            self._close_ticket_if_open(broker_symbol, opened_ticket, order_type, half)
            pos.ticket_tp1 = None
            pos.ticket_tp2 = None

        if pos.ticket_tp1 is None and pos.ticket_tp2 is None:
            return None

        self.positions[signal_id] = pos
        logger.info(
            "Opened MT5 position for signal #%s: %s %s vol=%.4f entry~%.5f sl=%.5f tp1=%.5f tp2=%.5f",
            signal_id, sig.direction, sig.symbol, pos.volume, price, sig.stop_loss, tps[0], tps[1],
        )
        return pos

    def _close_ticket_if_open(self, broker_symbol: str, ticket: int, opened_order_type, volume: float) -> None:
        """
        Best-effort cleanup: send an opposing market order against `ticket`
        to flatten a leg that opened successfully when its sibling leg (TP1
        or TP2) failed. Logs but does not raise on failure -- the caller
        already treats the signal as not-entered either way, and raising
        here would just crash the poll cycle over a cleanup step. If this
        fails, the position is still open on the broker and needs manual
        intervention; that's surfaced clearly in the log/error alert.
        """
        mt5 = _mt5()
        mt5_pos = self._find_open_mt5_position(ticket)
        if mt5_pos is None:
            # Already closed/never actually opened server-side -- nothing to do.
            return

        tick = mt5.symbol_info_tick(broker_symbol)
        if tick is None:
            logger.error(
                "Could not fetch tick for %s to close orphaned ticket %s -- "
                "position is still OPEN on the broker, close it manually.",
                broker_symbol, ticket,
            )
            return

        closing_type = mt5.ORDER_TYPE_SELL if opened_order_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        closing_price = tick.bid if closing_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": broker_symbol,
            "volume": volume,
            "type": closing_type,
            "position": ticket,
            "price": closing_price,
            "deviation": 20,
            "magic": self.magic_number,
            "comment": "smc_bot:orphan_leg_cleanup",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                "Failed to close orphaned ticket %s on %s: retcode=%s comment=%s -- "
                "position is still OPEN on the broker, close it manually.",
                ticket, broker_symbol, getattr(result, "retcode", None), getattr(result, "comment", None),
            )
        else:
            logger.info("Closed orphaned leg ticket %s on %s (cleanup after partial order failure).", ticket, broker_symbol)

    def _find_open_mt5_position(self, ticket: int):
        mt5 = _mt5()
        if ticket is None:
            return None
        positions = mt5.positions_get(ticket=ticket)
        return positions[0] if positions else None

    def manage_positions(self, df_ltf_by_symbol: Dict[str, pd.DataFrame]) -> list:
        """
        Call once per poll cycle. Returns a list of (signal_id, event) for
        anything that changed, so the caller can log it / send a Telegram
        update -- event in {"tp1_hit", "tp2_hit", "stopped_out", "trailed"}.
        """
        mt5 = _mt5()
        events = []
        for signal_id, pos in list(self.positions.items()):
            leg1 = self._find_open_mt5_position(pos.ticket_tp1)
            leg2 = self._find_open_mt5_position(pos.ticket_tp2)

            if leg1 is None and pos.ticket_tp1 is not None and not pos.tp1_hit:
                # TP1 leg closed (either hit TP or was stopped) -- figure out which
                pos.tp1_hit = True
                pos.trail_active = True
                events.append((signal_id, "tp1_hit_or_stopped"))
                if leg2 is not None:
                    self._move_sl(pos, pos.entry, leg2.ticket)
                    logger.info("Signal #%s: TP1 leg closed -- moved remaining SL to breakeven (%.5f)", signal_id, pos.entry)

            if leg1 is None and leg2 is None:
                del self.positions[signal_id]
                events.append((signal_id, "closed"))
                continue

            if pos.trail_active and leg2 is not None:
                df_ltf = df_ltf_by_symbol.get(pos.symbol)
                if df_ltf is not None and len(df_ltf) > 20:
                    new_sl = self._trailing_sl(pos, df_ltf)
                    if new_sl is not None:
                        current_sl = leg2.sl
                        improved = (
                            (pos.direction == "BUY" and new_sl > current_sl)
                            or (pos.direction == "SELL" and new_sl < current_sl)
                        )
                        if improved:
                            self._move_sl(pos, new_sl, leg2.ticket)
                            events.append((signal_id, "trailed"))

        return events

    def _trailing_sl(self, pos: ManagedPosition, df_ltf: pd.DataFrame) -> Optional[float]:
        p = pos.strategy_params
        pre = precompute_indicators(df_ltf, swing_left=p["swing_left"], swing_right=p["swing_right"], atr_period=p["atr_period"])
        atr_now = pre["atr"][-1] if len(pre["atr"]) else None
        if atr_now is None or pd.isna(atr_now):
            return None
        buf = p.get("sl_buffer_atr", 0.0) * atr_now

        right = p["swing_right"]
        if pos.direction == "BUY":
            idxs = [i for i in range(len(pre["is_low"]) - right) if pre["is_low"][i]]
            if not idxs:
                return None
            last_swing_low = pre["low"][idxs[-1]]
            return last_swing_low - buf
        else:
            idxs = [i for i in range(len(pre["is_high"]) - right) if pre["is_high"][i]]
            if not idxs:
                return None
            last_swing_high = pre["high"][idxs[-1]]
            return last_swing_high + buf

    def _move_sl(self, pos: ManagedPosition, new_sl: float, ticket: int) -> None:
        mt5 = _mt5()
        mt5_pos = self._find_open_mt5_position(ticket)
        if mt5_pos is None:
            return
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": mt5_pos.symbol,
            "position": ticket,
            "sl": new_sl,
            "tp": mt5_pos.tp,
            "magic": self.magic_number,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("Failed to move SL for ticket %s: %s", ticket, getattr(result, "comment", None))
