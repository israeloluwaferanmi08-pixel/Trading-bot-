"""
Live polling loop: fetch latest candles for each configured symbol, evaluate
the signal engine, and push new signals to Telegram.

Run with:  python -m smc_bot.live_bot

Data source per symbol: ccxt is tried first for symbols that have a
ccxt_symbol configured (BTCUSD via BTC/USDT, which is free/unmetered), and
falls back to TwelveData if ccxt fails or isn't configured for that symbol.
XAUUSD has no crypto-exchange equivalent, so it skips ccxt and is served by
TwelveData directly. (MT5 support has been removed from this deployment.)

Everything below signal-generation (SignalEngine.evaluate / Signal) is the
operational layer: health monitoring, error alerts, crash recovery,
duplicate protection, logging, IDs, reports, analytics, Telegram commands,
config reload, watchdog, API retry, message queueing, startup/shutdown
messages. None of it changes STRATEGY or how a signal is produced.
"""
import json
import logging
import os
import signal as signal_module
import sys
import time
from datetime import datetime, timezone

from . import alerts, config, health, outcomes, reports, twelvedata_feed, watchdog
from .commands import BotState, start_command_listener
from .data_feed import get_ccxt_data
from .notify_queue import QueuedNotifier
from .session import confluence_for, session_for
from .signals import SignalEngine
from .store import Store
from .telegram_notifier import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MAX_FETCH_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5


# --- existing JSON dedupe state (unchanged mechanism) ------------------------
def load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(path: str, state: dict) -> None:
    with open(path, "w") as f:
        json.dump(state, f)


def fetch_symbol_data(sym_cfg, timeframe: str, n_bars: int = 500):
    """ccxt -> TwelveData, each retried up to MAX_FETCH_RETRIES times before
    falling through to the next source (checklist item 14 — API failover).

    ccxt is tried first because it's free and unmetered. For BTCUSD (the
    only symbol with a ccxt_symbol configured), ccxt/Binance is used and
    TwelveData's metered credits are never spent on BTC at all. XAUUSD has
    no ccxt equivalent (ccxt_symbol is ""), so it falls straight through the
    ccxt block untouched and is served by TwelveData.
    """
    last_err = None

    if sym_cfg.ccxt_symbol:
        for attempt in range(1, MAX_FETCH_RETRIES + 1):
            try:
                logger.info("Trying ccxt (binance) for %s", sym_cfg.name)
                return get_ccxt_data("binance", sym_cfg.ccxt_symbol, timeframe, n_bars)
            except Exception as e:
                last_err = e
                logger.warning(
                    "ccxt fetch failed for %s (%s), attempt %d/%d: %s",
                    sym_cfg.name, timeframe, attempt, MAX_FETCH_RETRIES, e,
                )
                # HTTP 451 ("restricted location") is Binance permanently
                # geo-blocking this host — retrying with backoff can't fix
                # that, it just burns ~15s per timeframe every single poll
                # cycle before falling through to TwelveData anyway. Fail
                # over immediately instead.
                if "451" in str(e) or "restricted location" in str(e).lower():
                    logger.info(
                        "ccxt (binance) geo-blocked for %s — skipping retries, falling back to TwelveData",
                        sym_cfg.name,
                    )
                    break
                if attempt < MAX_FETCH_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS)

    if sym_cfg.twelvedata_symbol and config.TWELVEDATA_API_KEYS:
        for attempt in range(1, MAX_FETCH_RETRIES + 1):
            try:
                logger.info("Falling back to TwelveData for %s", sym_cfg.name)
                return twelvedata_feed.get_twelvedata_data(
                    sym_cfg.twelvedata_symbol, timeframe, n_bars,
                    config.TWELVEDATA_API_KEYS, config.TWELVEDATA_COOLDOWN_SECONDS,
                )
            except Exception as e:
                last_err = e
                logger.warning(
                    "TwelveData fetch failed for %s (%s), attempt %d/%d: %s",
                    sym_cfg.name, timeframe, attempt, MAX_FETCH_RETRIES, e,
                )
                if attempt < MAX_FETCH_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS)

    raise last_err if last_err else RuntimeError(f"No data source succeeded for {sym_cfg.name}")


def run_once(notifier, store, state: dict) -> dict:
    signals_today = 0
    for symbol, sym_cfg in config.SYMBOLS.items():
        try:
            df_ltf = fetch_symbol_data(sym_cfg, sym_cfg.ltf, n_bars=500)
            df_htf = fetch_symbol_data(sym_cfg, sym_cfg.htf, n_bars=500)
        except Exception as e:
            logger.error("Skipping %s this cycle — data fetch failed: %s", symbol, e)
            store.log_error(f"data_feed:{symbol}", str(e))
            notifier.send_message(alerts.error_message(f"Data feed ({symbol})", str(e)))
            continue

        # --- outcome tracking on existing open signals, using the fresh data
        # we just fetched anyway (checklist item 20) ---
        try:
            outcomes.check_outcomes(store, notifier, symbol, df_ltf)
        except Exception:
            logger.exception("Outcome tracking failed for %s", symbol)

        try:
            engine = SignalEngine(symbol, config.STRATEGY)
            already = set(tuple(x) for x in state.get(symbol, []))

            signals = engine.evaluate(df_ltf, df_htf, already_signaled_zone_ids=already)

            for sig in signals:
                confluence = confluence_for(sig)
                session = session_for(sig.bar_time)
                signal_id = store.log_signal(sig, confluence, session)

                message = (
                    f"Signal #{signal_id}\n"
                    + sig.to_message()
                    + "\n\n"
                    + "\n".join(confluence)
                )
                logger.info("New signal #%s: %s", signal_id, sig.to_message().replace("\n", " | "))
                notifier.send_message(message)
                already.add((sig.zone.formed_idx, sig.zone.kind))
                signals_today += 1

            state[symbol] = list(already)
        except Exception as e:
            logger.exception("Signal engine failed for %s", symbol)
            store.log_error(f"signal_engine:{symbol}", str(e))
            notifier.send_message(alerts.error_message(f"Signal engine ({symbol})", str(e)))
            continue

    health.mark_scan(ok=True)
    return state


def _today_signal_count(store) -> int:
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return store.count_signals_since(today_start.isoformat())


def main():
    store = Store(config.DB_PATH)
    restart_count = store.incr_meta("restart_count")

    raw_notifier = TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
    notifier = QueuedNotifier(raw_notifier, min_gap_seconds=config.TELEGRAM_MIN_GAP_SECONDS)
    if not notifier.enabled():
        logger.warning(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — signals will only be logged, not sent."
        )

    is_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
    notifier.send_now(
        alerts.startup_message(
            config.BOT_VERSION, list(config.SYMBOLS.keys()), notifier.enabled(), is_railway
        )
    )
    logger.info("Startup complete. Restart count this deployment lineage: %s", restart_count)

    state = load_state(config.STATE_FILE)

    saved_poll = store.get_meta("poll_seconds")
    poll_seconds = int(saved_poll) if saved_poll else config.POLL_SECONDS
    bot_state = BotState(poll_seconds, list(config.SYMBOLS.keys()), config.BOT_VERSION)

    if config.ADMIN_CHAT_ID and config.TELEGRAM_BOT_TOKEN:
        start_command_listener(config.TELEGRAM_BOT_TOKEN, config.ADMIN_CHAT_ID, bot_state, store, notifier)
    watchdog.start_watchdog(notifier, config.WATCHDOG_MINUTES)

    def _graceful_shutdown(signum, frame):
        logger.info("Received signal %s — shutting down gracefully.", signum)
        try:
            save_state(config.STATE_FILE, state)
            notifier.send_now(
                alerts.shutdown_message(
                    f"signal {signum}",
                    {"markets_scanned": health.markets_scanned(), "signals_today": _today_signal_count(store)},
                )
            )
        finally:
            sys.exit(0)

    signal_module.signal(signal_module.SIGTERM, _graceful_shutdown)
    signal_module.signal(signal_module.SIGINT, _graceful_shutdown)

    logger.info(
        "Starting live loop. Symbols: %s. Poll interval: %ss",
        list(config.SYMBOLS), bot_state.poll_seconds,
    )

    last_heartbeat = 0.0

    while True:
        if bot_state.restart_requested.is_set():
            os._exit(0)

        if bot_state.scanning.is_set():
            try:
                state = run_once(notifier, store, state)
                save_state(config.STATE_FILE, state)
            except Exception:
                logger.exception("Unexpected error in main loop; continuing.")
                store.log_error("main_loop", "unexpected top-level exception")
                notifier.send_message(alerts.error_message("Main loop", "unexpected exception — see logs"))
        else:
            health.mark_scan(ok=True)  # scanning paused isn't a health problem

        now = time.time()
        if now - last_heartbeat >= config.HEARTBEAT_MINUTES * 60:
            notifier.send_message(alerts.heartbeat_message(health.markets_scanned(), _today_signal_count(store)))
            last_heartbeat = now

        try:
            reports.maybe_send_daily(store, notifier)
            reports.maybe_send_weekly(store, notifier)
        except Exception:
            logger.exception("Report scheduling failed")

        time.sleep(bot_state.poll_seconds)


if __name__ == "__main__":
    main()
