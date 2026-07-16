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

from . import alerts, config, dashboard, drawdown, health, outcomes, reports, twelvedata_feed, watchdog
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


def _signal_progress(sig, df_ltf) -> float:
    """
    Fraction of the entry->TP1 distance already covered by the close of
    the bar the signal formed on. 0 (or negative, clamped to 0) if price
    closed at/against entry. Used to catch signals where most of the move
    a zone was supposed to catch already happened before the bar even
    closed and the alert could be sent — see config.STALE_SIGNAL_MAX_PROGRESS.
    """
    close = df_ltf["close"].iloc[sig.bar_idx]
    tp1 = sig.take_profits[0]
    total = (tp1 - sig.entry) if sig.direction == "BUY" else (sig.entry - tp1)
    if total <= 0:
        return 0.0
    moved = (close - sig.entry) if sig.direction == "BUY" else (sig.entry - close)
    return max(0.0, moved / total)


def run_once(notifier, store, state: dict, notify_cooldown_minutes: int = 30, enabled_symbols=None) -> dict:
    signals_today = 0
    cooldown_seconds = max(0, notify_cooldown_minutes) * 60
    # `_cooldown` is a reserved key inside the same state dict/file that
    # already tracks per-symbol signaled zone ids — maps "SYMBOL:DIRECTION"
    # -> epoch seconds of the last signal actually SENT to Telegram for
    # that pair. Distinct from zone dedup: this throttles notification
    # frequency regardless of which zone triggered the signal.
    cooldown_state = state.setdefault("_cooldown", {})
    now_ts = time.time()
    for symbol, sym_cfg in config.SYMBOLS.items():
        if enabled_symbols is not None and symbol not in enabled_symbols:
            continue
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
            drawdown.check_drawdown(
                store, notifier, symbol,
                initial_balance=config.BACKTEST["initial_balance"],
                risk_percent=sym_cfg.risk_percent,
                thresholds=config.DRAWDOWN_ALERT_THRESHOLDS_PCT,
            )
        except Exception:
            logger.exception("Drawdown check failed for %s", symbol)

        try:
            engine = SignalEngine(symbol, config.get_strategy_params(symbol))
            already = set(tuple(x) for x in state.get(symbol, []))

            signals = engine.evaluate(df_ltf, df_htf, already_signaled_zone_ids=already)

            # Every backtested number assumes at most LIVE_MAX_OPEN_PER_SYMBOL
            # open positions on this symbol at once (backtester default: 1).
            # Recomputed fresh each cycle from the store, then tracked locally
            # so two signals arriving in the same cycle don't both slip
            # through — see config.LIVE_MAX_OPEN_PER_SYMBOL and
            # alerts.skipped_concurrency_message for the full rationale.
            open_count = len(store.open_signals(symbol))
            max_open = config.LIVE_MAX_OPEN_PER_SYMBOL

            for sig in signals:
                confluence = confluence_for(sig)
                session = session_for(sig.bar_time)
                already.add((sig.zone.formed_idx, sig.zone.kind))

                # Stale/chased check: a signal only exists once its bar has
                # closed and this poll cycle picked it up — if price already
                # ran most of the way to TP1 within that same bar, sending
                # the alert now means chasing an entry near the top of the
                # move rather than catching the zone. See
                # config.STALE_SIGNAL_MAX_PROGRESS and alerts.stale_chased_message.
                progress = _signal_progress(sig, df_ltf)
                if progress > config.STALE_SIGNAL_MAX_PROGRESS:
                    stale_key = f"{symbol}:stale"
                    last_stale_sent = cooldown_state.get(stale_key, 0)
                    stale_suppressed = (now_ts - last_stale_sent) < cooldown_seconds

                    signal_id = store.log_signal(
                        sig, confluence, session, notified=not stale_suppressed, status="stale_chased",
                    )
                    logger.info(
                        "Signal #%s (%s) skipped — already %.0f%% of the way to TP1 by bar close (limit %.0f%%)%s",
                        signal_id, sig.symbol, progress * 100, config.STALE_SIGNAL_MAX_PROGRESS * 100,
                        " (notification suppressed)" if stale_suppressed else "",
                    )
                    if not stale_suppressed:
                        notifier.send_message(
                            alerts.stale_chased_message(sig, progress, config.STALE_SIGNAL_MAX_PROGRESS)
                        )
                        cooldown_state[stale_key] = now_ts
                    continue

                if open_count >= max_open:
                    # Skipped, not suppressed: doesn't occupy a slot, isn't
                    # tracked toward outcome stats — same as the backtester
                    # itself would have done with this signal. Always
                    # logged to the store for full visibility (dashboard/
                    # history), but the Telegram ping itself rides the same
                    # per-symbol notification cooldown as real signals so a
                    # choppy stretch that keeps re-triggering the zone
                    # detector while a position is open doesn't spam the
                    # chat — one heads-up per cooldown window is enough to
                    # know it's happening.
                    skip_key = f"{symbol}:skipped"
                    last_skip_sent = cooldown_state.get(skip_key, 0)
                    skip_suppressed = (now_ts - last_skip_sent) < cooldown_seconds

                    signal_id = store.log_signal(
                        sig, confluence, session, notified=not skip_suppressed, status="skipped_concurrency",
                    )
                    logger.info(
                        "Signal #%s (%s) skipped — %d/%d positions already open%s",
                        signal_id, sig.symbol, open_count, max_open,
                        " (notification suppressed)" if skip_suppressed else "",
                    )
                    if not skip_suppressed:
                        notifier.send_message(alerts.skipped_concurrency_message(sig, open_count, max_open))
                        cooldown_state[skip_key] = now_ts
                    continue

                cooldown_key = f"{symbol}:{sig.direction}"
                last_sent = cooldown_state.get(cooldown_key, 0)
                suppressed = (now_ts - last_sent) < cooldown_seconds

                signal_id = store.log_signal(sig, confluence, session, notified=not suppressed)
                open_count += 1  # this cycle's next signal (if any) sees it as open too

                if suppressed:
                    logger.info(
                        "Signal #%s (%s) suppressed — inside %sm notification cooldown for %s",
                        signal_id, sig.symbol, notify_cooldown_minutes, cooldown_key,
                    )
                    continue

                message = (
                    f"Signal #{signal_id}\n"
                    + sig.to_message()
                    + "\n\n"
                    + "\n".join(confluence)
                )
                logger.info("New signal #%s: %s", signal_id, sig.to_message().replace("\n", " | "))
                notifier.send_message(message)
                cooldown_state[cooldown_key] = now_ts
                signals_today += 1

            state[symbol] = list(already)
            state["_cooldown"] = cooldown_state
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
    saved_cooldown = store.get_meta("notify_cooldown_minutes")
    notify_cooldown_minutes = (
        int(saved_cooldown) if saved_cooldown is not None else config.NOTIFICATION_COOLDOWN_MINUTES
    )
    bot_state = BotState(
        poll_seconds, list(config.SYMBOLS.keys()), config.BOT_VERSION,
        notify_cooldown_minutes=notify_cooldown_minutes,
    )

    if config.ADMIN_CHAT_ID and config.TELEGRAM_BOT_TOKEN:
        start_command_listener(config.TELEGRAM_BOT_TOKEN, config.ADMIN_CHAT_ID, bot_state, store, notifier)
    watchdog.start_watchdog(notifier, config.WATCHDOG_MINUTES)

    if config.DASHBOARD_ENABLED:
        dashboard.start_dashboard(store, bot_state, notifier, config)

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
                state = run_once(
                    notifier, store, state,
                    notify_cooldown_minutes=bot_state.notify_cooldown_minutes,
                    enabled_symbols=bot_state.enabled_symbols,
                )
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

        sleep_until = time.time() + bot_state.poll_seconds
        while time.time() < sleep_until:
            if bot_state.force_scan.is_set():
                bot_state.force_scan.clear()
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
