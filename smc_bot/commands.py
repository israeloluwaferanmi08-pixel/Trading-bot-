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

Also handles: sending a chart screenshot (photo) triggers Gemini vision
analysis (grade, entry/SL/TP estimate, reasoning) -- separate feature
from the bot's own signals, purely an AI read of an image, not backed
by real price data. Results are saved to the chart_analyses table.

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
    def __init__(self, poll_seconds: int, symbols: list, version: str, notify_cooldown_minutes: int = 30):
        self.scanning = threading.Event()
        self.scanning.set()
        self.poll_seconds = poll_seconds
        self.symbols = symbols
        self.version = version
        self.restart_requested = threading.Event()
        self.notify_cooldown_minutes = notify_cooldown_minutes
        # Set by /scannow or the dashboard's "Scan Now" button — the main
        # loop's sleep checks this every second and breaks early instead of
        # waiting out the full poll interval. Cleared once the scan starts.
        self.force_scan = threading.Event()
        # Symbols currently eligible to be scanned. Starts as "all
        # configured symbols" — removing one here just skips it in
        # run_once(), it doesn't touch config.SYMBOLS or STRATEGY.
        self.enabled_symbols = set(symbols)


def _handle_command(text: str, state: BotState, store, notifier) -> str:
    cmd, *rest = text.strip().split()
    cmd = cmd.lower()

    if cmd == "/status":
        status = "Scanning ✅" if state.scanning.is_set() else "Paused ⏸"
        disabled = [s for s in state.symbols if s not in state.enabled_symbols]
        return (
            f"Status: {status}\n"
            f"Uptime: {health.uptime_str()}\n"
            f"Last scan: {health.last_scan_str()}\n"
            f"Markets scanned: {health.markets_scanned()}\n"
            f"Memory: {health.memory_mb()} MB | CPU: {health.cpu_percent()}%\n"
            f"Poll interval: {state.poll_seconds}s\n"
            f"Notification cooldown: {state.notify_cooldown_minutes}m per symbol+direction\n"
            f"Disabled markets: {', '.join(disabled) if disabled else 'none'}"
        )
    if cmd == "/stats":
        return alerts.performance_message(store.performance_stats())
    if cmd == "/signals":
        rows = store.recent_signals(10)
        if not rows:
            return "No signals logged yet."
        lines = [
            f"#{r['id']} {r['symbol']} {r['direction']} — {r['status']}"
            + ("" if r["notified"] else " 🔕 (cooldown-suppressed)")
            for r in rows
        ]
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
    if cmd == "/scannow":
        state.force_scan.set()
        return "Forcing an immediate scan cycle... ⏱"
    if cmd == "/enable":
        if not rest or rest[0].upper() not in state.symbols:
            return f"Usage: /enable <symbol>  ({', '.join(state.symbols)})"
        state.enabled_symbols.add(rest[0].upper())
        return f"{rest[0].upper()} enabled ✅"
    if cmd == "/disable":
        if not rest or rest[0].upper() not in state.symbols:
            return f"Usage: /disable <symbol>  ({', '.join(state.symbols)})"
        state.enabled_symbols.discard(rest[0].upper())
        return f"{rest[0].upper()} disabled ⏸ (skipped in every scan cycle until re-enabled)"
    if cmd == "/setinterval":
        if not rest or not rest[0].isdigit():
            return "Usage: /setinterval <seconds>"
        state.poll_seconds = max(5, int(rest[0]))
        store.set_meta("poll_seconds", state.poll_seconds)
        return f"Poll interval set to {state.poll_seconds}s"
    if cmd == "/setcooldown":
        if not rest or not rest[0].isdigit():
            return "Usage: /setcooldown <minutes>  (0 disables throttling)"
        state.notify_cooldown_minutes = max(0, int(rest[0]))
        store.set_meta("notify_cooldown_minutes", state.notify_cooldown_minutes)
        return (
            f"Notification cooldown set to {state.notify_cooldown_minutes}m. "
            f"Repeat signals for the same symbol+direction inside that window will be "
            f"logged but not sent to Telegram."
        )
    if cmd == "/reload":
        saved = store.get_meta("poll_seconds")
        if saved:
            state.poll_seconds = int(saved)
        saved_cd = store.get_meta("notify_cooldown_minutes")
        if saved_cd is not None:
            state.notify_cooldown_minutes = int(saved_cd)
        return (
            f"Reloaded operational settings. Poll interval: {state.poll_seconds}s | "
            f"Notification cooldown: {state.notify_cooldown_minutes}m"
        )
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
            "/scannow - force an immediate scan cycle\n"
            "/enable <symbol> - resume scanning one market\n"
            "/disable <symbol> - skip one market without pausing everything\n"
            "/setinterval <sec> - change poll interval live\n"
            "/setcooldown <min> - change notification cooldown live\n"
            "/reload - re-apply saved operational settings\n"
            "/restart - hard restart the process\n"
            "/ask <question> - ask Gemini about the bot's stats/signals\n\n"
            "Send a chart screenshot (photo) to get an AI read: grade, "
            "entry/SL/TP estimate, reasoning. This is a separate AI feature, "
            "not the bot's real signals -- treat it as a second opinion, not "
            "precise tradeable numbers."
        )
    return "Unknown command. Try /help"


def _handle_chart_photo(base_url: str, photos: list, store, notifier) -> None:
    """Download a chart screenshot the user sent and analyze it with Gemini
    vision, replying with a formatted result. Best-effort: any failure here
    should end in a Telegram message telling the user what went wrong,
    never a silent drop or a crash of the whole listener loop."""
    try:
        # Telegram sends multiple sizes; the last entry is the largest.
        file_id = photos[-1]["file_id"]

        file_info = requests.get(f"{base_url}/getFile", params={"file_id": file_id}, timeout=20).json()
        file_path = file_info.get("result", {}).get("file_path")
        if not file_path:
            notifier.send_now("Couldn't retrieve that photo from Telegram -- try sending it again.")
            return

        token = base_url.rsplit("/bot", 1)[-1]
        download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        img_resp = requests.get(download_url, timeout=30)
        img_resp.raise_for_status()
        image_bytes = img_resp.content

        mime_type = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"

        notifier.send_now("📷 Got your chart -- analyzing now, one moment...")

        verdict, raw_text = gemini_assistant.analyze_chart_image(
            config.GEMINI_API_KEY, config.GEMINI_MODEL, image_bytes, mime_type
        )

        if verdict is None:
            notifier.send_now(
                "Couldn't get a clean read on that chart. This happens sometimes -- "
                "try a clearer screenshot, or try again in a moment."
            )
            logger.warning("Chart analysis parse failure, raw response: %s", raw_text[:500])
            return

        store.log_chart_analysis(telegram_user_id=None, verdict=verdict, raw_response=raw_text)
        notifier.send_now(gemini_assistant.format_chart_analysis_for_telegram(verdict))

    except requests.RequestException as e:
        logger.warning("Chart photo handling failed (network): %s", e)
        notifier.send_now("Couldn't process that chart right now -- network issue, try again shortly.")
    except Exception:
        logger.exception("Chart photo handling failed unexpectedly")
        notifier.send_now("Something went wrong analyzing that chart. Try again, or a different screenshot.")


def start_command_listener(bot_token: str, admin_chat_id: str, state: BotState, store, notifier):
    def _loop():
        base_url = f"https://api.telegram.org/bot{bot_token}"
        offset = None
        saved = store.get_meta("telegram_update_offset")
        if saved:
            offset = int(saved)

        # getUpdates (long-polling) and an active webhook are mutually
        # exclusive on Telegram's side — if a webhook was ever registered
        # for this bot token (e.g. from an earlier test/deploy), every
        # getUpdates call fails with 409 Conflict and commands silently
        # never arrive. Clear any webhook before we start polling so this
        # can't block us.
        try:
            wh = requests.get(f"{base_url}/getWebhookInfo", timeout=15).json()
            if wh.get("result", {}).get("url"):
                logger.warning("Removing stale Telegram webhook so getUpdates can work: %s", wh["result"]["url"])
                requests.get(f"{base_url}/deleteWebhook", timeout=15)
        except requests.RequestException as e:
            logger.warning("Command listener: could not check/clear webhook: %s", e)

        consecutive_failures = 0
        conflict_alert_sent = False

        while True:
            try:
                params = {"timeout": 25}
                if offset is not None:
                    params["offset"] = offset
                resp = requests.get(f"{base_url}/getUpdates", params=params, timeout=30)
                if resp.status_code == 409:
                    # Another poller (old Railway instance, local test run,
                    # or a webhook we couldn't clear) is holding the
                    # long-poll connection for this bot token.
                    consecutive_failures += 1
                    logger.warning(
                        "Command listener: 409 Conflict from getUpdates — another process is polling "
                        "this bot token, or a webhook is still set. (failure #%d)", consecutive_failures,
                    )
                    if consecutive_failures == 3 and not conflict_alert_sent:
                        conflict_alert_sent = True
                        try:
                            notifier.send_now(
                                "⚠️ Telegram commands aren't working: another process (old Railway "
                                "instance, local test run, or a webhook) is already polling this bot "
                                "token, so getUpdates keeps getting rejected with 409 Conflict. Make sure "
                                "only one instance of this bot is running, and that no webhook is set."
                            )
                        except Exception:
                            pass
                    time.sleep(5)
                    continue
                resp.raise_for_status()
                consecutive_failures = 0
                updates = resp.json().get("result", [])
                for upd in updates:
                    offset = upd["update_id"] + 1
                    store.set_meta("telegram_update_offset", offset)
                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != str(admin_chat_id):
                        continue

                    photos = msg.get("photo")
                    if photos:
                        _handle_chart_photo(base_url, photos, store, notifier)
                        continue

                    text = msg.get("text", "")
                    if not text.startswith("/"):
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
