"""
Daily & weekly report generation (checklist items 8/9/10), plus a
scheduler helper that fires them at the right time exactly once, even
across restarts (tracked via store meta so a redeploy at 23:59 doesn't
double-send).
"""
import logging
from datetime import datetime, timedelta, timezone

from . import alerts

logger = logging.getLogger(__name__)


def _day_start_iso(d: datetime) -> str:
    return d.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def build_daily_stats(store, day: datetime) -> dict:
    since = _day_start_iso(day)
    rows = store.signals_since(since)
    rows = [r for r in rows if r["sent_at"] < _day_start_iso(day + timedelta(days=1))]
    by_symbol = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], 0)
        by_symbol[r["symbol"]] += 1
    best_pair = max(by_symbol, key=by_symbol.get) if by_symbol else None
    worst_pair = min(by_symbol, key=by_symbol.get) if by_symbol else None
    return {
        "total": len(rows),
        "buy": len([r for r in rows if r["direction"] == "BUY"]),
        "sell": len([r for r in rows if r["direction"] == "SELL"]),
        "best_pair": best_pair,
        "worst_pair": worst_pair,
    }


def build_weekly_stats(store, week_start: datetime):
    since = _day_start_iso(week_start)
    until = _day_start_iso(week_start + timedelta(days=7))
    rows = store.signals_since(since)
    rows = [r for r in rows if r["sent_at"] < until]
    by_symbol = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], 0)
        by_symbol[r["symbol"]] += 1
    return {"total": len(rows)}, by_symbol


def maybe_send_daily(store, notifier) -> None:
    now = datetime.now(timezone.utc)
    today_key = now.strftime("%Y-%m-%d")
    if store.get_meta("last_daily_report") == today_key:
        return
    if now.hour != 0:
        return
    yesterday = now - timedelta(days=1)
    stats = build_daily_stats(store, yesterday.replace(hour=0, minute=0, second=0, microsecond=0))
    notifier.send_message(alerts.daily_report_message(yesterday.strftime("%Y-%m-%d"), stats))
    store.set_meta("last_daily_report", today_key)


def maybe_send_weekly(store, notifier) -> None:
    now = datetime.now(timezone.utc)
    if now.weekday() != 6:  # Sunday
        return
    week_key = now.strftime("%Y-W%W")
    if store.get_meta("last_weekly_report") == week_key:
        return
    if now.hour != 0:
        return
    week_start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    stats, by_symbol = build_weekly_stats(store, week_start)
    notifier.send_message(alerts.weekly_report_message(week_key, stats, by_symbol))
    store.set_meta("last_weekly_report", week_key)
