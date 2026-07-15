"""
Two small, purely descriptive helpers:

  * session_for(dt)  — labels a UTC timestamp as Asia/London/New York/Overlap
                        for logging & "best/worst session" stats.
  * confluence_for(sig) — turns the actual boolean conditions your strategy
                        already required to fire (zone kind, dealing-range
                        position, HTF trend, impulse strength) into a
                        human-readable checklist for the signal message.

Neither function changes which signals fire or their entry/SL/TP — they
only describe, after the fact, a signal signals.py already decided to send.
"""
from datetime import datetime, timezone


def session_for(dt) -> str:
    if dt is None:
        return "unknown"
    try:
        if not isinstance(dt, datetime):
            dt = datetime.fromisoformat(str(dt))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hour = dt.astimezone(timezone.utc).hour
    except Exception:
        return "unknown"

    if 0 <= hour < 7:
        return "Asia"
    if 7 <= hour < 12:
        return "London"
    if 12 <= hour < 16:
        return "London/NY overlap"
    if 16 <= hour < 21:
        return "New York"
    return "Asia"


def confluence_for(sig) -> list:
    """
    Builds the checklist from fields already present on the Signal object.
    Every fired signal satisfies all of these by construction (they're the
    AND conditions in signals.py) — this is a transparency readout of *why*
    it fired, not an independent confidence score. We label it that way in
    the message so it isn't mistaken for a probability estimate.
    """
    checks = []
    checks.append(f"✔ {sig.zone.kind.title()} zone tapped")
    checks.append(f"✔ Zone in {sig.dealing_range_position.title()} of range")
    checks.append(f"✔ HTF trend {sig.htf_trend.title()} (aligned)")
    rr = "/".join(f"{r:.1f}R" for r in sig.risk_reward)
    checks.append(f"✔ Target(s): {rr}")
    return checks
