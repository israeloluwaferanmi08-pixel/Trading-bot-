"""
Central configuration.

All secrets (Telegram token, API keys) are read from environment variables
so nothing sensitive lives in source code. Copy .env.example to .env and
fill in your own values, or export the variables in your shell.
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()  # loads .env if present, silently does nothing otherwise


@dataclass
class SymbolConfig:
    name: str                 # display name, e.g. "XAUUSD"
    ccxt_symbol: str           # e.g. "BTC/USDT" (used only for BTCUSD via crypto exchange)
    twelvedata_symbol: str = ""  # e.g. "XAU/USD"
    ltf: str = "M15"           # entry / lower timeframe
    htf: str = "H4"            # higher timeframe used for trend filter
    pip_size: float = 0.01     # smallest price increment relevant for SL buffer
    risk_percent: float = 1.0  # % of account risked per trade


SYMBOLS = {
    "XAUUSD": SymbolConfig(
        name="XAUUSD",
        ccxt_symbol="",          # gold isn't on crypto exchanges
        twelvedata_symbol="XAU/USD",
        ltf="M15",
        htf="H4",
        pip_size=0.1,
        risk_percent=1.0,
    ),
    "BTCUSD": SymbolConfig(
        name="BTCUSD",
        # ccxt/Binance is geo-blocked (HTTP 451) from this host on every
        # single poll — confirmed in production logs, both timeframes,
        # every cycle, with zero exceptions. It always falls through to
        # TwelveData anyway, so trying it first just adds latency and
        # noisy log lines for no benefit. Left blank to skip straight to
        # TwelveData. If you ever move hosts (or switch to a
        # non-geo-restricted exchange like Kraken) this can be
        # re-enabled — see data_feed.get_ccxt_data for the ccxt call.
        ccxt_symbol="",
        twelvedata_symbol="BTC/USD",
        ltf="M15",
        htf="H4",
        pip_size=1.0,
        risk_percent=1.0,
    ),
}

# --- Strategy parameters -----------------------------------------------
STRATEGY = dict(
    swing_left=2,             # bars to the left required to confirm a fractal swing point
    swing_right=2,            # bars to the right required to confirm a fractal swing point
    min_impulse_atr=1.5,      # a leg must travel >= this many ATR to count as a valid impulse
    atr_period=14,
    zone_lookback=300,        # how many LTF candles to scan for zones
    dealing_range_lookback=100,   # bars used to build the premium/discount range
    equilibrium_buffer=0.0,   # % buffer around 50% to treat as "equilibrium / no trade"
    max_zone_age_bars=250,    # a zone older than this (untouched) is dropped as stale
    sl_buffer_atr=0.15,       # stop loss buffer beyond zone edge, in ATR
    tp_r_multiples=(2.0, 3.0),  # take-profit targets expressed in R multiples
    htf_ema_fast=50,
    htf_ema_slow=200,

    # --- LTF market-structure confirmation (BOS/CHoCH) -----------------
    # Extra AND condition on top of the zone + HTF-trend rules in
    # signals.py: a BUY additionally requires the LTF (entry timeframe)
    # to currently be in a bullish BOS/CHoCH structure trend, and a SELL
    # requires bearish. It only ever vetoes a signal that already passed
    # the zone/HTF-trend rules — it can't fire one on its own. Uses the
    # same swing_left/swing_right fractal settings as zone detection.
    # Set to False to go back to the original two-condition rule set.
    require_structure_confirmation=True,
    structure_lookback=300,   # bars of history scanned for BOS/CHoCH state; defaults to zone_lookback if omitted

    # --- Loss-cluster cooldown ---------------------------------------
    # After a trade is stopped out at a loss, block any new signal whose
    # entry price sits within `loss_cooldown_atr_mult` * ATR of that
    # losing trade's entry, for `loss_cooldown_bars` bars afterward.
    #
    # Why: in a range-bound/choppy market the HTF EMA trend filter can
    # stay stale (e.g. still "bullish" after a sharp prior rally) while
    # price just oscillates. The zone detector then keeps finding fresh
    # zones in the same narrow price band and re-triggering the same
    # losing trade over and over. This cooldown stops it from re-entering
    # right where it just got stopped out, without needing a full
    # trend-regime classifier. Tested as robust across atr_mult 2.0-3.0
    # and bars 48-192 on XAUUSDT Dec 2025-Apr 2026 data — set to 0 / None
    # to disable.
    loss_cooldown_atr_mult=2.0,
    loss_cooldown_bars=48,

    # --- Consecutive-loss (streak) cooldown ---------------------------
    # After `consecutive_loss_limit` losing trades close back-to-back (a
    # win resets the count), ALL new signals are blocked for
    # `consecutive_loss_cooldown_bars` bars — a blunt circuit breaker for
    # "something about current conditions isn't working", independent of
    # price (unlike the ATR cooldown above). Set consecutive_loss_limit
    # to 0/None to disable. 96 bars on M15 = 1 day.
    # Backtested on real BTCUSD M15 (Dec 2024-Jun 2025): limit=2 cut max
    # drawdown from 22.6% -> 18.6% and *improved* win rate/PF/return too
    # (both in- and out-of-sample) — limits of 3-4 rarely triggered often
    # enough on this data to matter. 96 bars beat 48/192/384 on return
    # while matching the drawdown improvement. Re-check if you change
    # symbol/timeframe/risk since the right threshold is data-dependent.
    consecutive_loss_limit=2,
    consecutive_loss_cooldown_bars=96,
)

# --- Telegram -------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Chat allowed to issue /commands. Defaults to TELEGRAM_CHAT_ID so a single
# .env value is enough for a personal bot; set separately if you ever want
# signals broadcast to a channel but commands restricted to you.
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", TELEGRAM_CHAT_ID)

# --- Live loop --------------------------------------------------------
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", "sent_signals.json")

# --- Smart notification filtering --------------------------------------
# Zone-level dedup (already_signaled_zone_ids in signals.py) stops the exact
# same zone firing twice, but separate zones that form close together in
# price/time can still each produce a "new" signal within minutes of each
# other. This cooldown throttles Telegram notifications per (symbol,
# direction) pair — a signal that fires again for the same symbol+direction
# before the cooldown elapses is still logged (so /stats stays accurate)
# but is NOT sent to Telegram. Change live with /setcooldown <minutes>.
NOTIFICATION_COOLDOWN_MINUTES = int(os.getenv("NOTIFICATION_COOLDOWN_MINUTES", "30"))

# --- Operational layer (does NOT affect strategy logic) -----------------
# All of these are ops/monitoring knobs only — signal generation itself is
# governed entirely by STRATEGY above and is untouched by any of this.
BOT_VERSION = os.getenv("BOT_VERSION", "1.0")
DB_PATH = os.getenv("DB_PATH", "data/bot_state.db")
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))
WATCHDOG_MINUTES = int(os.getenv("WATCHDOG_MINUTES", "20"))
TELEGRAM_MIN_GAP_SECONDS = float(os.getenv("TELEGRAM_MIN_GAP_SECONDS", "1.2"))

# --- TwelveData (live price data for symbols/timeframes ccxt can't serve) --
# Up to 3 free-tier keys, rotated automatically: when one gets rate-limited
# (HTTP 429 or a TwelveData "code":429 body), it's put on cooldown and the
# next key is tried instead. Leave keys 2/3 blank to run with just one.
TWELVEDATA_API_KEYS = [
    k for k in (
        os.getenv("TWELVEDATA_API_KEY_1", ""),
        os.getenv("TWELVEDATA_API_KEY_2", ""),
        os.getenv("TWELVEDATA_API_KEY_3", ""),
    ) if k
]
TWELVEDATA_COOLDOWN_SECONDS = int(os.getenv("TWELVEDATA_COOLDOWN_SECONDS", "60"))

# --- Gemini (Telegram /ask assistant only — read-only, no trading impact) --
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# --- Backtest -----------------------------------------------------------
# NOTE: initial_balance is in CENTS (a "cent account" style setup) — 1000
# cents = the equivalent of a $10 real-money account, scaled up. R-multiple
# math and % returns work identically regardless of the unit; only the
# absolute balance numbers printed are in cents.
BACKTEST = dict(
    initial_balance=1_000.0,
    risk_percent=5.0,           # % risked per trade
    spread_pips=2,               # cost model, in pip_size units
    slippage_pips=1,
)
