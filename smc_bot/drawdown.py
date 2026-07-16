"""
Live drawdown tracking + alerting.

Distinct from outcomes.py's per-trade SL/TP notifications: this tracks the
*cumulative* account picture — how far the running balance has fallen from
its highest point so far — not any single trade's result. You can lose 5
SLs in a row and be deep in a drawdown, or lose 5 SLs scattered among
enough wins to barely dent your peak; a single SL alert can't tell the
difference, this can.

Computes a running balance per symbol using the EXACT SAME compounding
formula as Backtester.run() (see backtester.py's "proper, race-free
balance accounting" block):

    risk_amount = balance * risk_percent / 100
    balance += realized_r * risk_amount

applied in chronological order over that symbol's closed trades, starting
from BACKTEST['initial_balance']. This makes a live drawdown reading
directly comparable to a backtested one — e.g. if BTCUSD's worst
backtested drawdown was 56.6%, live crossing past that is a genuine signal
something about current conditions differs from what was tested, not an
artifact of the two being measured differently.

Purely observational: reads closed signal history, never feeds back into
signal generation, position sizing, or any strategy decision.
"""
import json
import logging

from . import alerts

logger = logging.getLogger(__name__)


def compute_running_balance(closed_rows, initial_balance: float, risk_percent: float) -> list:
    """
    `closed_rows` must already be ordered oldest-first (closed_at ASC).
    Returns a list of {id, closed_at, balance} — balance after each trade,
    mirroring backtester.py's compounding formula exactly.
    """
    balance = initial_balance
    risk_pct = risk_percent / 100.0
    out = []
    for row in closed_rows:
        r = row["outcome_r"] or 0.0
        risk_amount = balance * risk_pct
        balance += r * risk_amount
        out.append({"id": row["id"], "closed_at": row["closed_at"], "balance": balance})
    return out


def current_drawdown(balances: list):
    """
    Returns (current_balance, peak_balance, drawdown_pct) from a list of
    running-balance points as produced by compute_running_balance().
    drawdown_pct is 0 if balances is empty or currently at a new high.
    """
    if not balances:
        return None, None, 0.0
    peak = balances[0]["balance"]
    for b in balances:
        peak = max(peak, b["balance"])
    current = balances[-1]["balance"]
    dd_pct = max(0.0, (peak - current) / peak * 100) if peak > 0 else 0.0
    return current, peak, dd_pct


def check_drawdown(
    store,
    notifier,
    symbol: str,
    initial_balance: float,
    risk_percent: float,
    thresholds,
) -> None:
    """
    Call once per symbol per scan cycle (cheap — a small indexed SQL scan,
    not a hot-path cost). Recomputes running balance from full closed-trade
    history, and alerts on any threshold newly crossed since the last time
    this symbol made a new equity high.

    Persisted in the store's meta table (survives restarts) under
    "dd_alerted:{symbol}" as a JSON list of thresholds already alerted
    during the CURRENT drawdown episode. Reset to [] the moment the symbol
    makes a new equity high, so a later drawdown re-crosses fresh and
    re-alerts rather than staying silent forever after the first time.
    """
    if not thresholds:
        return
    rows = [r for r in store.closed_signals_ordered() if r["symbol"] == symbol]
    if not rows:
        return

    balances = compute_running_balance(rows, initial_balance, risk_percent)
    current, peak, dd_pct = current_drawdown(balances)
    if current is None:
        return

    meta_key = f"dd_alerted:{symbol}"
    alerted = set(json.loads(store.get_meta(meta_key, "[]")))

    if current >= peak:
        # New (or tied) equity high — this drawdown episode is over.
        if alerted:
            store.set_meta(meta_key, json.dumps([]))
        return

    newly_crossed = sorted(t for t in thresholds if dd_pct >= t and t not in alerted)
    if not newly_crossed:
        return

    worst = newly_crossed[-1]
    try:
        notifier.send_message(alerts.drawdown_alert_message(symbol, dd_pct, worst, current, peak))
    except Exception:
        logger.exception("Failed to send drawdown alert for %s", symbol)

    alerted.update(newly_crossed)
    store.set_meta(meta_key, json.dumps(sorted(alerted)))
