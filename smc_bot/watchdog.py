"""
Watchdog (checklist item 13): if no scan has completed successfully within
`limit_minutes`, log + alert + hard-exit the process. Railway's restart
policy (see railway.json, restartPolicyType=ALWAYS) then relaunches it
fresh. This is a last-resort safety net for a hang the per-cycle
try/except in live_bot.py didn't catch (e.g. a deadlocked network call).
"""
import logging
import os
import threading
import time

from . import alerts, health

logger = logging.getLogger(__name__)


def start_watchdog(notifier, limit_minutes: int, check_every_seconds: int = 60) -> threading.Thread:
    def _loop():
        while True:
            time.sleep(check_every_seconds)
            stale_seconds = health.seconds_since_last_scan()
            if stale_seconds > limit_minutes * 60:
                minutes = stale_seconds / 60
                logger.critical("Watchdog: no scan in %.1f min — restarting.", minutes)
                try:
                    notifier.send_now(alerts.watchdog_message(minutes, limit_minutes))
                except Exception:
                    logger.exception("Watchdog: failed to send alert before restart")
                os._exit(1)  # hard exit; Railway restart policy relaunches the process

    t = threading.Thread(target=_loop, daemon=True, name="watchdog")
    t.start()
    return t
