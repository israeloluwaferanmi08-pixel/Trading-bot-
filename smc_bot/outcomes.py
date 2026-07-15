"""
Trade outcome tracking (checklist item 20).

Every scan cycle, for each symbol, we already fetch fresh LTF candles to
look for new signals. This module reuses that same data to check whether
any still-open signal for that symbol has since hit its stop loss, its
first take-profit target, or gone stale (expired). Purely observational —
does not feed back into signal generation.
"""
import json
import logging
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger(__name__)

# A signal that's gone this many LTF bars without hitting SL or TP1 is
# considered stale and marked "expired" rather than tracked forever.
EXPIRY_BARS = 400


def check_outcomes(store, notifier, symbol: str, df_ltf: pd.DataFrame) -> None:
    open_rows = store.open_signals(symbol)
    if not open_rows or df_ltf is None or df_ltf.empty:
        return

    recent = df_ltf  # already the latest n_bars fetched this cycle
    last_time = recent["time"].iloc[-1] if "time" in recent.columns else None

    for row in open_rows:
        direction = row["direction"]
        sl = row["stop_loss"]
        tps = json.loads(row["take_profits"])
        tp1 = tps[0]

        sent_at = datetime.fromisoformat(row["sent_at"])
        bars_elapsed = None
        bars_since = recent
        if last_time is not None and "time" in recent.columns:
            sent_time = pd.to_datetime(sent_at.replace(tzinfo=None))
            bars_since = recent[recent["time"] > sent_time]
            bars_elapsed = len(bars_since)

        # Only candles that have formed *since this signal was sent* count
        # toward SL/TP checks. Previously hi/lo were taken over the whole
        # fetched window (up to 500 bars), so price action from BEFORE the
        # signal even existed could immediately trigger a false SL/TP hit.
        if bars_since.empty:
            continue
        hi = bars_since["high"].max()
        lo = bars_since["low"].min()

        hit_sl = False
        hit_tp = False
        if direction == "BUY":
            hit_sl = lo <= sl
            hit_tp = hi >= tp1
        else:
            hit_sl = hi >= sl
            hit_tp = lo <= tp1

        # If both look "hit" within the same coarse window we can't know
        # which came first from just min/max — conservatively call it a
        # loss (SL) since that's the safer assumption for stats.
        if hit_sl and hit_tp:
            hit_tp = False

        if hit_sl:
            r = -1.0
            store.close_signal(row["id"], "sl_hit", r)
            _notify(notifier, row, "sl_hit", r)
        elif hit_tp:
            entry = row["entry"]
            risk = abs(entry - sl)
            r = abs(tp1 - entry) / risk if risk else 0.0
            store.close_signal(row["id"], "tp_hit", r)
            _notify(notifier, row, "tp_hit", r)
        elif bars_elapsed is not None and bars_elapsed >= EXPIRY_BARS:
            store.close_signal(row["id"], "expired", 0.0)
            _notify(notifier, row, "expired", 0.0)


def _notify(notifier, row, status: str, r: float) -> None:
    from . import alerts

    try:
        notifier.send_message(
            alerts.outcome_message(row["id"], row["symbol"], row["direction"], status, r)
        )
    except Exception:
        logger.exception("Failed to send outcome notification")
