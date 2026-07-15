"""
In-process health metrics: uptime, memory, CPU, scan counters.

Pure operational telemetry — reads nothing from and writes nothing to the
strategy/signal logic.
"""
import time
from datetime import datetime, timezone

try:
    import psutil
    _process = psutil.Process()
except ImportError:  # psutil not installed — degrade gracefully
    psutil = None
    _process = None

_start_time = time.time()
_markets_scanned = 0
_last_scan_time: float = 0.0
_last_scan_ok = True


def mark_scan(ok: bool = True) -> None:
    global _last_scan_time, _last_scan_ok, _markets_scanned
    _last_scan_time = time.time()
    _last_scan_ok = ok
    _markets_scanned += 1


def uptime_str() -> str:
    secs = int(time.time() - _start_time)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def seconds_since_last_scan() -> float:
    if _last_scan_time == 0.0:
        return time.time() - _start_time
    return time.time() - _last_scan_time


def last_scan_str() -> str:
    if _last_scan_time == 0.0:
        return "never yet"
    return datetime.fromtimestamp(_last_scan_time, tz=timezone.utc).strftime("%H:%M UTC")


def memory_mb() -> float:
    if _process is None:
        return -1.0
    return round(_process.memory_info().rss / (1024 * 1024), 1)


def cpu_percent() -> float:
    if _process is None:
        return -1.0
    return _process.cpu_percent(interval=0.1)


def markets_scanned() -> int:
    return _markets_scanned


def is_healthy() -> bool:
    return _last_scan_ok
