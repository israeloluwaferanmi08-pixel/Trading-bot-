"""
Turn a Backtester.run() result into human-readable performance stats.
"""
from typing import Dict, List


def summarize(result: Dict) -> Dict:
    trades = [t for t in result["trades"] if t.closed]
    n = len(trades)
    if n == 0:
        return {"total_trades": 0, "message": "No trades were generated over this period."}

    wins = [t for t in trades if t.realized_r > 0]
    losses = [t for t in trades if t.realized_r <= 0]

    gross_win_r = sum(t.realized_r for t in wins)
    gross_loss_r = abs(sum(t.realized_r for t in losses))

    starting_balance = result.get("initial_balance", result["final_balance"])
    balances = [e["balance"] for e in result["equity_curve"]]
    peak = starting_balance
    max_dd = 0.0
    for b in balances:
        peak = max(peak, b)
        dd = (peak - b) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    return {
        "total_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / n, 2),
        "avg_r": round(sum(t.realized_r for t in trades) / n, 3),
        "profit_factor": round(gross_win_r / gross_loss_r, 2) if gross_loss_r > 0 else float("inf"),
        "total_r": round(sum(t.realized_r for t in trades), 2),
        "starting_balance": round(starting_balance, 2),
        "final_balance": round(result["final_balance"], 2),
        "return_pct": round(100 * (result["final_balance"] - starting_balance) / starting_balance, 2)
        if starting_balance else 0.0,
        "max_drawdown_pct": round(100 * max_dd, 2),
        "long_trades": len([t for t in trades if t.direction == "BUY"]),
        "short_trades": len([t for t in trades if t.direction == "SELL"]),
    }


def print_summary(result: Dict, symbol: str = "") -> None:
    s = summarize(result)
    print(f"\n=== Backtest summary {symbol} ===")
    if s["total_trades"] == 0:
        print(s["message"])
        return
    print(f"Total trades      : {s['total_trades']}  (long {s['long_trades']} / short {s['short_trades']})")
    print(f"Win rate          : {s['win_rate_pct']}%  ({s['wins']}W / {s['losses']}L)")
    print(f"Avg R per trade   : {s['avg_r']}")
    print(f"Total R           : {s['total_r']}")
    print(f"Profit factor     : {s['profit_factor']}")
    print(f"Final balance     : {s['final_balance']}  (return {s['return_pct']}%)")
    print(f"Max drawdown      : {s['max_drawdown_pct']}%")
