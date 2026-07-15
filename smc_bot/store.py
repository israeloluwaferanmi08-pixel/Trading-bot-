"""
Operational data store (SQLite).

This is layered ON TOP of the strategy — it does not feed anything back
into signal generation. It exists purely so the bot can:

  * hand out a stable, incrementing Signal ID
  * log every signal with full context (for later analysis)
  * track what happened to each signal afterward (TP / SL / expired)
  * answer /status, /stats, /signals, daily & weekly report queries
  * remember operational counters (restart count, last scan time) across
    restarts, since Railway's filesystem is ephemeral unless you attach a
    Volume (see README) — without a Volume this resets on every redeploy.

None of this touches smc_bot/signals.py, zones.py, trend.py or the
strategy parameters in config.STRATEGY.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

_lock = threading.RLock()


def _connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = _connect(db_path)
        self._init_schema()

    def _init_schema(self):
        with _lock, self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profits TEXT NOT NULL,
                    zone_kind TEXT,
                    zone_top REAL,
                    zone_bottom REAL,
                    range_position TEXT,
                    htf_trend TEXT,
                    session TEXT,
                    confluence TEXT,
                    bar_time TEXT,
                    sent_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',   -- open | tp_hit | sl_hit | expired
                    outcome_r REAL,
                    closed_at TEXT,
                    notified INTEGER NOT NULL DEFAULT 1    -- 0 if suppressed by the notification cooldown
                )
                """
            )
            # Migration for DBs created before the `notified` column existed.
            existing_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(signals)")}
            if "notified" not in existing_cols:
                self.conn.execute(
                    "ALTER TABLE signals ADD COLUMN notified INTEGER NOT NULL DEFAULT 1"
                )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    module TEXT,
                    reason TEXT,
                    occurred_at TEXT NOT NULL
                )
                """
            )

    # --- meta key/value helpers -----------------------------------------
    def get_meta(self, key: str, default=None):
        with _lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        with _lock, self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def incr_meta(self, key: str, by: int = 1) -> int:
        with _lock:
            current = int(self.get_meta(key, 0) or 0)
            new_val = current + by
            self.set_meta(key, new_val)
            return new_val

    # --- signals ----------------------------------------------------------
    def log_signal(self, sig, confluence: list, session: str, notified: bool = True) -> int:
        with _lock, self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO signals
                (symbol, direction, entry, stop_loss, take_profits, zone_kind,
                 zone_top, zone_bottom, range_position, htf_trend, session,
                 confluence, bar_time, sent_at, status, notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    sig.symbol,
                    sig.direction,
                    sig.entry,
                    sig.stop_loss,
                    json.dumps(sig.take_profits),
                    sig.zone.kind,
                    sig.zone.top,
                    sig.zone.bottom,
                    sig.dealing_range_position,
                    sig.htf_trend,
                    session,
                    json.dumps(confluence),
                    str(sig.bar_time),
                    datetime.now(timezone.utc).isoformat(),
                    1 if notified else 0,
                ),
            )
            return cur.lastrowid

    def open_signals(self, symbol: Optional[str] = None):
        with _lock:
            if symbol:
                return self.conn.execute(
                    "SELECT * FROM signals WHERE status = 'open' AND symbol = ?", (symbol,)
                ).fetchall()
            return self.conn.execute("SELECT * FROM signals WHERE status = 'open'").fetchall()

    def close_signal(self, signal_id: int, status: str, outcome_r: float) -> None:
        with _lock, self.conn:
            self.conn.execute(
                "UPDATE signals SET status = ?, outcome_r = ?, closed_at = ? WHERE id = ?",
                (status, outcome_r, datetime.now(timezone.utc).isoformat(), signal_id),
            )

    def log_error(self, module: str, reason: str) -> None:
        with _lock, self.conn:
            self.conn.execute(
                "INSERT INTO errors (module, reason, occurred_at) VALUES (?, ?, ?)",
                (module, reason, datetime.now(timezone.utc).isoformat()),
            )

    # --- reporting queries --------------------------------------------------
    def signals_since(self, since_iso: str):
        with _lock:
            return self.conn.execute(
                "SELECT * FROM signals WHERE sent_at >= ? ORDER BY sent_at", (since_iso,)
            ).fetchall()

    def count_signals_since(self, since_iso: str) -> int:
        with _lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE sent_at >= ?", (since_iso,)
            ).fetchone()
            return row["n"] if row else 0

    def recent_signals(self, limit: int = 10):
        with _lock:
            return self.conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def performance_stats(self):
        """Win rate / avg R etc. over CLOSED signals only (tp_hit or sl_hit)."""
        with _lock:
            rows = self.conn.execute(
                "SELECT * FROM signals WHERE status IN ('tp_hit', 'sl_hit')"
            ).fetchall()
        n = len(rows)
        if n == 0:
            return {"closed_trades": 0}
        wins = [r for r in rows if r["status"] == "tp_hit"]
        losses = [r for r in rows if r["status"] == "sl_hit"]
        avg_r = sum((r["outcome_r"] or 0) for r in rows) / n
        by_symbol = {}
        for r in rows:
            by_symbol.setdefault(r["symbol"], []).append(r["outcome_r"] or 0)
        best_symbol = max(by_symbol, key=lambda s: sum(by_symbol[s])) if by_symbol else None
        worst_symbol = min(by_symbol, key=lambda s: sum(by_symbol[s])) if by_symbol else None
        by_session = {}
        for r in rows:
            by_session.setdefault(r["session"] or "unknown", []).append(r["outcome_r"] or 0)
        best_session = max(by_session, key=lambda s: sum(by_session[s]) / len(by_session[s])) if by_session else None
        worst_session = min(by_session, key=lambda s: sum(by_session[s]) / len(by_session[s])) if by_session else None
        return {
            "closed_trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100 * len(wins) / n, 1),
            "avg_r": round(avg_r, 2),
            "best_symbol": best_symbol,
            "worst_symbol": worst_symbol,
            "best_session": best_session,
            "worst_session": worst_session,
        }
