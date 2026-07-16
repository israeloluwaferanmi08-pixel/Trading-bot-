#!/usr/bin/env python3
"""
Windows-VPS entrypoint: runs the SAME SignalEngine/STRATEGY as live_bot.py,
but sources OHLCV data from a running MT5 terminal (instead of
ccxt/TwelveData) and, on every new signal, places and manages a real order
in that MT5 account -- so no manual step is needed to act on a signal.

This is a SEPARATE process from live_bot.py, not a replacement for it:
  - live_bot.py:      Linux/Railway, Telegram alerts only, no execution.
  - mt5_live_bot.py:  Windows VPS, same strategy, ALSO executes in MT5.

Run only one of them against a given symbol at a time unless you want
duplicate Telegram alerts for the same signal. If you want Telegram alerts
AND MT5 execution, run this file alone -- it sends its own alerts too (see
alerts.py, reused as-is).

Setup: see MT5_VPS_SETUP.md. Requires:
  - A Windows VPS with the MT5 terminal installed and a demo account logged in
  - `pip install -r requirements-mt5.txt` (adds the MetaTrader5 package)
  - MT5_LOGIN / MT5_PASSWORD / MT5_SERVER set in .env (or the terminal
    already logged in and left that way -- MT5_LOGIN can be left as 0 to
    just attach to whatever's already logged in)

Usage:
    python mt5_live_bot.py
"""
import logging
import os
import signal as signal_module
import sys
import time

from smc_bot import alerts, config
from smc_bot.mt5_executor import MT5Executor
from smc_bot.session import confluence_for, session_for
from smc_bot.signals import SignalEngine
from smc_bot.store import Store
from smc_bot.telegram_notifier import TelegramNotifier
from smc_bot.notify_queue import QueuedNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_stop = False


def _handle_stop(signum, frame):
    global _stop
    logger.info("Received signal %s -- shutting down after this cycle.", signum)
    _stop = True


def run_cycle(mt5_feed, store, notifier, executor, broker_symbols, already_signaled):
    df_ltf_by_symbol = {}

    for symbol, sym_cfg in config.SYMBOLS.items():
        broker_symbol = broker_symbols[symbol]
        try:
            df_ltf = mt5_feed.get_mt5_data(broker_symbol, sym_cfg.ltf, n_bars=500)
            df_htf = mt5_feed.get_mt5_data(broker_symbol, sym_cfg.htf, n_bars=500)
        except Exception as e:
            logger.error("Data fetch failed for %s (%s): %s", symbol, broker_symbol, e)
            store.log_error(f"mt5_data_feed:{symbol}", str(e))
            continue

        df_ltf_by_symbol[symbol] = df_ltf

        try:
            engine = SignalEngine(symbol, config.get_strategy_params(symbol))
            already = already_signaled.setdefault(symbol, set())
            signals = engine.evaluate(df_ltf, df_htf, already_signaled_zone_ids=already)

            open_count = len(store.open_signals(symbol))
            max_open = config.LIVE_MAX_OPEN_PER_SYMBOL

            for sig in signals:
                already.add((sig.zone.formed_idx, sig.zone.kind))

                if open_count >= max_open:
                    logger.info("Skipping signal for %s -- %d/%d positions already open.", symbol, open_count, max_open)
                    store.log_signal(sig, confluence_for(sig), session_for(sig.bar_time), notified=False, status="skipped_concurrency")
                    continue

                signal_id = store.log_signal(sig, confluence_for(sig), session_for(sig.bar_time), notified=True)
                open_count += 1

                message = f"Signal #{signal_id} (MT5 auto-execute)\n" + sig.to_message() + "\n\n" + "\n".join(confluence_for(sig))
                notifier.send_message(message)

                strategy_params = config.get_strategy_params(symbol)
                balance = mt5_feed.account_balance()
                risk_amount = balance * (sym_cfg.risk_percent / 100.0)

                pos = executor.open_trade(signal_id, sig, broker_symbol, risk_amount, strategy_params)
                if pos is None:
                    notifier.send_message(f"⚠️ Signal #{signal_id}: MT5 order failed to open -- see logs.")
                else:
                    notifier.send_message(
                        f"✅ Signal #{signal_id}: opened {sig.direction} {symbol} "
                        f"vol={pos.volume:.4f} risking ~{risk_amount:.2f} account currency."
                    )

        except Exception as e:
            logger.exception("Signal engine failed for %s", symbol)
            store.log_error(f"signal_engine:{symbol}", str(e))
            continue

    try:
        events = executor.manage_positions(df_ltf_by_symbol)
        for signal_id, event in events:
            logger.info("Position event: signal #%s -> %s", signal_id, event)
            if event == "tp1_hit_or_stopped":
                notifier.send_message(f"Signal #{signal_id}: TP1 leg closed, remaining position moved to breakeven and now trailing.")
            elif event == "closed":
                notifier.send_message(f"Signal #{signal_id}: position fully closed.")
    except Exception:
        logger.exception("Position management failed this cycle")


def main():
    if not config.MT5_LOGIN or not config.MT5_PASSWORD or not config.MT5_SERVER:
        logger.warning(
            "MT5_LOGIN/MT5_PASSWORD/MT5_SERVER not fully set -- will try to attach to an "
            "already-logged-in terminal instead. Set all three in .env for a clean start."
        )

    # Imported here (not at module top) so this file gives a clear error on
    # non-Windows hosts instead of failing on an unrelated import earlier.
    from smc_bot import mt5_feed

    mt5_feed.connect(config.MT5_LOGIN, config.MT5_PASSWORD, config.MT5_SERVER, config.MT5_TERMINAL_PATH)

    broker_symbols = {}
    for symbol, sym_cfg in config.SYMBOLS.items():
        broker_symbols[symbol] = mt5_feed.resolve_symbol(sym_cfg.name, config.MT5_SYMBOL_CANDIDATES.get(symbol, []))
        logger.info("Resolved %s -> broker symbol '%s'", symbol, broker_symbols[symbol])

    store = Store(config.DB_PATH)
    raw_notifier = TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
    notifier = QueuedNotifier(raw_notifier, min_gap_seconds=config.TELEGRAM_MIN_GAP_SECONDS)
    if not notifier.enabled():
        logger.warning("Telegram not configured -- MT5 execution will still run, alerts will only be logged.")

    executor = MT5Executor(magic_number=config.MT5_MAGIC_NUMBER, demo_only=config.MT5_DEMO_ONLY)

    notifier.send_now(f"🚀 mt5_live_bot starting. Symbols: {list(broker_symbols.values())}. Demo-only: {config.MT5_DEMO_ONLY}.")

    signal_module.signal(signal_module.SIGTERM, _handle_stop)
    signal_module.signal(signal_module.SIGINT, _handle_stop)

    already_signaled = {}
    logger.info("Starting MT5 live loop. Poll interval: %ss", config.MT5_POLL_SECONDS)

    try:
        while not _stop:
            try:
                run_cycle(mt5_feed, store, notifier, executor, broker_symbols, already_signaled)
            except Exception:
                logger.exception("Unexpected error in MT5 main loop; continuing.")
                store.log_error("mt5_main_loop", "unexpected top-level exception")

            sleep_until = time.time() + config.MT5_POLL_SECONDS
            while time.time() < sleep_until and not _stop:
                time.sleep(1)
    finally:
        notifier.send_now("🛑 mt5_live_bot shutting down.")
        mt5_feed.disconnect()


if __name__ == "__main__":
    sys.exit(main())
