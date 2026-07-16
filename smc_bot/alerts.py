"""
Formatting for every *operational* Telegram message (heartbeat, errors,
startup/shutdown, daily/weekly reports). Signal messages themselves still
come from Signal.to_message() in signals.py — untouched.
"""
from datetime import datetime, timezone

from . import health


def startup_message(version: str, symbols: list, telegram_ok: bool, railway: bool) -> str:
    markets = "\n".join(f"  • {s}" for s in symbols)
    return (
        "🌲 SILVER BOT Started\n"
        f"Version: {version}\n"
        f"Markets:\n{markets}\n"
        f"Railway: {'Connected' if railway else 'Local run'}\n"
        f"Telegram: {'Connected' if telegram_ok else 'NOT configured'}\n"
        "Status: Ready ✅"
    )


def shutdown_message(reason: str, stats: dict) -> str:
    return (
        "🛑 SILVER BOT Shutting Down\n"
        f"Reason: {reason}\n"
        f"Uptime: {health.uptime_str()}\n"
        f"Markets scanned: {stats.get('markets_scanned', 0)}\n"
        f"Signals today: {stats.get('signals_today', 0)}\n"
        "State saved. Bye 👋"
    )


def heartbeat_message(markets_scanned: int, signals_today: int) -> str:
    healthy = health.is_healthy()
    return (
        "🟢 SILVER BOT Online\n\n"
        f"Uptime:\n{health.uptime_str()}\n\n"
        f"Markets scanned:\n{markets_scanned}\n\n"
        f"Signals today:\n{signals_today}\n\n"
        f"Last scan:\n{health.last_scan_str()}\n\n"
        f"Memory:\n{health.memory_mb()} MB\n\n"
        f"CPU:\n{health.cpu_percent()}%\n\n"
        f"Status:\n{'Healthy ✅' if healthy else 'Degraded ⚠️'}"
    )


def error_message(module: str, reason: str, restarting: bool = False) -> str:
    return (
        "🚨 SILVER BOT Error\n\n"
        f"Module:\n{module}\n\n"
        f"Reason:\n{reason}\n\n"
        f"Time:\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}\n\n"
        + ("Bot restarting..." if restarting else "Bot continuing — this cycle skipped.")
    )


def watchdog_message(minutes_stale: float, watchdog_limit: int) -> str:
    return (
        "🐕 Watchdog Triggered\n\n"
        f"No successful scan in {minutes_stale:.1f} min "
        f"(limit: {watchdog_limit} min).\n"
        "Restarting process now..."
    )


def drawdown_alert_message(symbol: str, dd_pct: float, threshold: float, balance: float, peak: float) -> str:
    """
    Fired by drawdown.check_drawdown() the first time a symbol's running
    (simulated, compounding) balance crosses a new drawdown threshold since
    its last equity high. This is a PORTFOLIO-level reading, not a single
    trade — distinct from outcome_message()'s per-trade SL/TP notice. See
    drawdown.py's module docstring for how "balance" is computed and why
    it's directly comparable to backtested max-drawdown figures.
    """
    severity = "🟠" if threshold < 50 else "🔴"
    return (
        f"{severity} {symbol} drawdown alert — {dd_pct:.1f}% below peak\n"
        f"Crossed the {threshold:.0f}% threshold.\n"
        f"Peak: {peak:,.2f} -> Current: {balance:,.2f}\n"
        "This tracks cumulative account balance, not any single trade — "
        "check /stats or the dashboard for the trades behind it."
    )


def skipped_concurrency_message(sig, open_count: int, max_open: int) -> str:
    """
    A signal cleared the strategy's own rules but wasn't sent as an
    actionable alert because {open_count} position(s) are already open on
    this symbol and LIVE_MAX_OPEN_PER_SYMBOL={max_open} — every backtest
    number in this repo assumes at most one open trade per symbol at a
    time, so taking this on top of an existing position would be trading
    a materially riskier (untested, deeper-drawdown) variant of the
    strategy. Logged with status='skipped_concurrency' for visibility —
    it does not occupy an open-position slot and its outcome isn't tracked,
    same as a signal the backtester itself would have skipped.
    """
    arrow = "🟢 BUY" if sig.direction == "BUY" else "🔴 SELL"
    return (
        f"⚠️ Signal skipped — {open_count}/{max_open} positions already open on {sig.symbol}\n"
        f"{arrow} {sig.symbol} @ {sig.entry:.2f} would have been sent, "
        "but taking it now means running MORE than one concurrent position "
        "on this symbol — a riskier pattern than what was backtested.\n"
        "Not logged as an open trade; not counted toward your performance stats."
    )


def outcome_message(signal_id: int, symbol: str, direction: str, status: str, outcome_r: float) -> str:
    icon = {"tp_hit": "🟢", "sl_hit": "🔴", "expired": "⚪"}.get(status, "⏳")
    label = {"tp_hit": "TP hit", "sl_hit": "SL hit", "expired": "Expired"}.get(status, status)
    sign = "+" if outcome_r >= 0 else ""
    return f"{icon} Signal #{signal_id} {symbol} {direction} — {label} ({sign}{outcome_r:.1f}R)"


def daily_report_message(date_str: str, stats: dict) -> str:
    return (
        "📊 Daily Summary\n\n"
        f"Date:\n{date_str}\n\n"
        f"Signals:\n{stats['total']}\n\n"
        f"BUY:\n{stats['buy']}\n\n"
        f"SELL:\n{stats['sell']}\n\n"
        f"Best pair:\n{stats.get('best_pair', 'n/a')}\n\n"
        f"Worst pair:\n{stats.get('worst_pair', 'n/a')}"
    )


def weekly_report_message(week_label: str, stats: dict, per_symbol: dict) -> str:
    lines = "\n".join(f"{sym}:\n{count}\n" for sym, count in per_symbol.items())
    return (
        "📅 Weekly Summary\n\n"
        f"Week:\n{week_label}\n\n"
        f"Signals:\n{stats['total']}\n\n"
        f"{lines}"
    )


def performance_message(stats: dict) -> str:
    if stats.get("closed_trades", 0) == 0:
        return "📈 Performance Analytics\n\nNo closed signals yet (all still open or none tracked)."
    return (
        "📈 Performance Analytics\n\n"
        f"Closed signals:\n{stats['closed_trades']}\n\n"
        f"Win rate:\n{stats['win_rate_pct']}% ({stats['wins']}W / {stats['losses']}L)\n\n"
        f"Avg R:\n{stats['avg_r']}\n\n"
        f"Best symbol:\n{stats.get('best_symbol', 'n/a')}\n\n"
        f"Worst symbol:\n{stats.get('worst_symbol', 'n/a')}\n\n"
        f"Best session:\n{stats.get('best_session', 'n/a')}\n\n"
        f"Worst session:\n{stats.get('worst_session', 'n/a')}"
    )
