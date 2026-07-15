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
        "🌲 Forest Bot Started\n"
        f"Version: {version}\n"
        f"Markets:\n{markets}\n"
        f"Railway: {'Connected' if railway else 'Local run'}\n"
        f"Telegram: {'Connected' if telegram_ok else 'NOT configured'}\n"
        "Status: Ready ✅"
    )


def shutdown_message(reason: str, stats: dict) -> str:
    return (
        "🛑 Forest Bot Shutting Down\n"
        f"Reason: {reason}\n"
        f"Uptime: {health.uptime_str()}\n"
        f"Markets scanned: {stats.get('markets_scanned', 0)}\n"
        f"Signals today: {stats.get('signals_today', 0)}\n"
        "State saved. Bye 👋"
    )


def heartbeat_message(markets_scanned: int, signals_today: int) -> str:
    healthy = health.is_healthy()
    return (
        "🟢 Forest Bot Online\n\n"
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
        "🚨 Forest Bot Error\n\n"
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
