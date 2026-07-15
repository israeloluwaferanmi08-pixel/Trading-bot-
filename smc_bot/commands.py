"""
Telegram command interface (checklist items 11/12).

Runs a long-poll loop against Telegram's getUpdates in a background
thread. Only messages from the configured admin chat are honored, so a
stranger who somehow messages your bot can't control it.

Commands:
  /status      - health snapshot (uptime, last scan, mem/cpu)
  /stats       - performance analytics (win rate, avg R, best/worst)
  /signals     - last 10 signals sent
  /logs        - last 10 errors logged
  /startscan   - resume scanning (bot stays up, just skips cycles)
  /stopscan    - pause scanning without killing the process
  /setinterval <seconds> - change POLL_SECONDS live, no redeploy
  /reload      - re-read the operational settings above from disk
  /restart     - hard-restart the process (Railway relaunches it)
  /help        - list commands

None of this can change strategy parameters (config.STRATEGY) — only
operational knobs (poll interval, scan on/off).
"""
import logging
import os
import threading
import time

import requests

from . import alerts, config, gemini_assistant, health

logger = logging.getLogger(__name__)


class BotState:
    def __init__(self, poll_seconds: int, symbols: list, version: str):
        self.scanning = threading.Event()
        self.scanning.set()
        self.poll_seconds = poll_seconds
        self.symbols = symbols
        self.version = version
        self.restart_requested = threading.Event()


def _handle_command(text: str, state: BotState, store, notifier) -> str:
    cmd, *rest = text.strip().split()
    cmd = cmd.lower()

    if cmd == "/status":
        status = "Scanning ✅" if state.scanning.is_set() else "Paused ⏸"
        return (
            f"Status: {status}\n"
            f"Uptime: {health.uptime_str()}\n"
            f"Last scan: {health.last_scan_str()}\n"
            f"Markets scanned: {health.markets_scanned()}\n"
            f"Memory: {health.memory_mb()} MB | CPU: {health.cpu_percent()}%\n"
            f"Poll interval: {state.poll_seconds}s"
        )
    if cmd == "/stats":
        return alerts.performance_message(store.performance_stats())
    if cmd == "/signals":
        rows = store.recent_signals(10)
        if not rows:
            return "No signals logged yet."
        lines = [f"#{r['id']} {r['symbol']} {r['direction']} — {r['status']}" for r in rows]
        return "Last signals:\n" + "\n".join(lines)
    if cmd == "/logs":
        errs = store.conn.execute(
            "SELECT * FROM errors ORDER BY id DESC LIMIT 10"
        ).fetchall()
        if not errs:
            return "No errors logged. 🎉"
        lines = [f"[{e['occurred_at']}] {e['module']}: {e['reason']}" for e in errs]
        return "Recent errors:\n" + "\n".join(lines)
    if cmd == "/startscan":
        state.scanning.set()
        return "Scanning resumed ✅"
    if cmd == "/stopscan":
        state.scanning.clear()
        return "Scanning paused ⏸ (bot stays online, will keep responding to commands)"
    if cmd == "/setinterval":
        if not rest or not rest[0].isdigit():
            return "Usage: /setinterval <seconds>"
        state.poll_seconds = max(5, int(rest[0]))
        store.set_meta("poll_seconds", state.poll_seconds)
        return f"Poll interval set to {state.poll_seconds}s"
    if cmd == "/reload":
        saved = store.get_meta("poll_seconds")
        if saved:
            state.poll_seconds = int(saved)
        return f"Reloaded operational settings. Poll interval: {state.poll_seconds}s"
    if cmd == "/restart":
        state.restart_requested.set()
        return "Restarting now... 🔄"
    if cmd == "/ask":
        if not rest:
            return "Usage: /ask <question>  e.g. /ask how many signals today?"
        question = " ".join(rest)
        context_text = gemini_assistant.build_context(store, state.symbols)
        return gemini_assistant.ask(config.GEMINI_API_KEY, config.GEMINI_MODEL, question, context_text)
    if cmd == "/help":
        return (
            "Commands:\n"
            "/status - health snapshot\n"
            "/stats - performance analytics\n"
            "/signals - last 10 signals\n"
            "/logs - last 10 errors\n"
            "/startscan - resume scanning\n"
            "/stopscan - pause scanning\n"
            "/setinterval <sec> - change poll interval live\n"
            "/reload - re-apply saved operational settings\n"
            "/restart - hard restart the process\n"
            "/ask <question> - ask Gemini about the bot's stats/signals"
        )
    return "Unknown command. Try /help"


def start_command_listener(bot_token: str, admin_chat_id: str, state: BotState, store, notifier):
    def _loop():
        base_url = f"https://api.telegram.org/bot{bot_token}"
        offset = None
        saved = store.get_meta("telegram_update_offset")
        if saved:
            offset = int(saved)
        while True:
            try:
                params = {"timeout": 25}
                if offset is not None:
                    params["offset"] = offset
                resp = requests.get(f"{base_url}/getUpdates", params=params, timeout=30)
                resp.raise_for_status()
                updates = resp.json().get("result", [])
                for upd in updates:
                    offset = upd["update_id"] + 1
                    store.set_meta("telegram_update_offset", offset)
                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text", "")
                    if chat_id != str(admin_chat_id) or not text.startswith("/"):
                        continue
                    reply = _handle_command(text, state, store, notifier)
                    notifier.send_now(reply)
                    if state.restart_requested.is_set():
                        time.sleep(1)
                        os._exit(0)
            except requests.RequestException as e:
                logger.warning("Command listener: getUpdates failed: %s", e)
                time.sleep(5)
            except Exception:
                logger.exception("Command listener: unexpected error")
                time.sleep(5)

    t = threading.Thread(target=_loop, daemon=True, name="telegram-commands")
    t.start()
    return t
