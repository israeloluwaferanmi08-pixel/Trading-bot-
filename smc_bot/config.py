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
        risk_percent=5.0,
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
        risk_percent=5.0,
    ),
}

# --- Strategy parameters -----------------------------------------------
# STRATEGY holds the shared/base params. Some params are tuned per-symbol
# (see STRATEGY_OVERRIDES below) because backtesting showed they don't
# transfer well across symbols — use get_strategy_params(symbol) rather
# than reading STRATEGY directly so you always get the right blend.
STRATEGY = dict(
    swing_left=2,             # bars to the left required to confirm a fractal swing point
    swing_right=2,            # bars to the right required to confirm a fractal swing point
    min_impulse_atr=1.5,      # a leg must travel >= this many ATR to count as a valid impulse
    atr_period=14,
    zone_lookback=300,        # how many LTF candles to scan for zones
    dealing_range_lookback=100,   # bars used to build the premium/discount range
    equilibrium_buffer=0.0,   # % buffer around 50% to treat as "equilibrium / no trade"
    max_zone_age_bars=250,    # a zone older than this (untouched) is dropped as stale
    sl_buffer_atr=0.3,       # stop loss buffer beyond zone edge, in ATR
    tp_r_multiples=(2.0, 3.0),  # take-profit targets expressed in R multiples

    # --- Entry depth into zone ------------------------------------------
    # 0.0 (default, unchanged behavior) = fill at the zone's OUTER edge
    # the instant price taps it at all (z.top for demand, z.bottom for
    # supply) — this is the widest possible SL, since risk = the entire
    # zone height, and it's the earliest possible fill.
    # 1.0 = fill at the zone's INNER edge (z.bottom for demand, z.top for
    # supply) — tightest possible SL (just the buffer), but requires price
    # to retrace all the way through the zone before firing, and won't
    # fire at all if price reverses before reaching that deep.
    # Anything between (e.g. 0.5 = mid-zone) trades off SL size against
    # fill probability. Only changes WHERE inside an already-tapped zone
    # the entry sits — it doesn't touch zone detection, mitigation, or
    # the premium/discount and HTF-trend filters above it.
    # Set to 0.25 after A/B backtesting on real XAUUSD data (17mo):
    # win rate 45.2%->51.1%, PF 1.65->2.06, total R 110.8->172.7,
    # max DD 56.6%->50.6%, avg SL distance 13.47->10.38. 0.5 was tested
    # too but gave no extra edge over 0.25 for a tighter (more fragile)
    # stop, so 0.25 was picked as the better risk/reward point, not 0.5.
    entry_depth_pct=0.25,

    # --- Structure-based trailing stop (post-TP1) ----------------------
    # Off by default so existing backtests/behavior are unchanged unless
    # explicitly opted into. When enabled, ONLY affects the runner after
    # TP1 (the existing move-to-breakeven-at-TP1 behavior is unchanged) —
    # from then on, the stop trails behind the most recently *confirmed*
    # swing low (BUY) / swing high (SELL), using the same swing_left/
    # swing_right fractal already used for zone detection, offset by
    # sl_buffer_atr (same buffer used on the initial SL) so the stop
    # doesn't sit exactly on the pivot. The stop only ever tightens —
    # never loosens back past breakeven. Look-ahead safe: a swing at
    # index j is only used once bar j+swing_right has closed, and the
    # backtester only applies a newly confirmed swing starting the NEXT
    # bar after it was confirmed (same convention as new signal entries).
    # Set to True after A/B backtesting stacked on top of entry_depth_pct=0.25
    # (17mo real XAUUSD): PF 2.06->2.15, total R 172.7->192.8, max DD
    # 50.6%->48.4%, win rate flat (51.05->50.88, noise). Free improvement —
    # no added risk, no fewer trades — so kept on by default.
    trail_stop_after_tp1=True,
    htf_ema_fast=50,
    htf_ema_slow=200,

    # --- LTF market-structure confirmation (BOS/CHoCH) -----------------
    # Extra AND condition on top of the zone + HTF-trend rules in
    # signals.py: a BUY additionally requires the LTF (entry timeframe)
    # to currently be in a bullish BOS/CHoCH structure trend, and a SELL
    # requires bearish. It only ever vetoes a signal that already passed
    # the zone/HTF-trend rules — it can't fire one on its own. Uses the
    # same swing_left/swing_right fractal settings as zone detection.
    #
    # Backtested per-symbol (see STRATEGY_OVERRIDES): this filter helps on
    # BTCUSD (raises win rate/PF, cuts drawdown roughly in half) but hurt
    # every metric on XAUUSD in our sample (Feb-Apr 2026) — fewer trades,
    # lower PF, and a *worse* drawdown, not better. Default here is off;
    # BTCUSD's own tuning showed it's a net win there too once you're
    # optimizing for drawdown-adjusted return rather than raw return alone
    # — but we're going with raw-return-optimized BTC per the last round
    # of backtests, so it's off for both symbols right now. Flip back to
    # True for BTCUSD in STRATEGY_OVERRIDES if you want the lower-drawdown
    # BTC variant instead (87 trades/54% WR/PF 2.6/DD 18.6% vs 168
    # trades/50.6% WR/PF 2.17/DD 38.6% with it off).
    require_structure_confirmation=False,
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
    # to disable. Shared across both symbols; unlike the streak cooldown
    # below, this one wasn't part of the BOS/CHoCH-era changes.
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
    # This one was tuned on BTCUSD specifically — see STRATEGY_OVERRIDES,
    # which turns it off for XAUUSD (it wasn't part of the pre-BOS/CHoCH
    # XAU config that backtested best on our XAU sample).
    consecutive_loss_limit=2,
    consecutive_loss_cooldown_bars=96,
)

# --- Per-symbol strategy overrides --------------------------------------
# Backtesting (Feb-Apr 2026 XAUUSD, Dec 2024-Jun 2025 BTCUSD, both real
# data) showed these two symbols want different filter sets — what helps
# one hurts the other. Rather than silently picking one global config,
# each symbol's differences from the STRATEGY base are spelled out here.
# Always fetch params via get_strategy_params(symbol), never read
# STRATEGY directly, so you get the right blend for the symbol you're
# trading.
STRATEGY_OVERRIDES = {
    "XAUUSD": dict(
        # Pre-BOS/CHoCH config: no structure filter, no streak cooldown.
        # Backtest (Feb-Apr 2026, 5% risk): 51 trades, 47.06% win rate,
        # PF 1.78, +145.5% return, 18.55% max DD. The ATR loss cooldown
        # above (loss_cooldown_atr_mult/bars) still applies — it was
        # already on in this config's best backtest.
        require_structure_confirmation=False,
        consecutive_loss_limit=0,
        consecutive_loss_cooldown_bars=0,
    ),
    "BTCUSD": dict(
        # Current/BOS-era config with the structure filter turned back
        # off — chosen for raw return over drawdown-adjusted return.
        # Backtest (Dec 2024-Jun 2025, 5% risk): 168 trades, 50.6% win
        # rate, PF 2.17, +6,758% return, 38.63% max DD. Both cooldowns
        # (ATR + consecutive-loss streak) are active. If you'd rather
        # have the lower-drawdown variant instead (87 trades, 54.0% WR,
        # PF 2.6, +1,666% return, 18.55% max DD), set
        # require_structure_confirmation=True here.
        require_structure_confirmation=False,
    ),
}


def get_strategy_params(symbol: str) -> dict:
    """Return STRATEGY merged with this symbol's overrides (if any)."""
    return {**STRATEGY, **STRATEGY_OVERRIDES.get(symbol, {})}

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

# --- Web dashboard -----------------------------------------------------
# Read-only-by-default operational dashboard (#1 from the roadmap gap
# list) — runs in a background thread inside this same process, backed by
# the same SQLite Store. Does not touch signal generation or STRATEGY.
DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").lower() in ("1", "true", "yes")
# Railway sets $PORT automatically for services with public networking
# enabled; DASHBOARD_PORT is a manual override for local runs.
DASHBOARD_PORT = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8080")))
# If unset, a random token is generated at startup and sent to your
# Telegram admin chat (and logged) — see dashboard.py. Set this explicitly
# if you want a stable URL across restarts.
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
# How long a dashboard login stays valid before you need to re-enter the
# token. A stolen/leaked session cookie is only useful for this long.
DASHBOARD_SESSION_HOURS = int(os.getenv("DASHBOARD_SESSION_HOURS", "12"))
# Railway serves the app over HTTPS at the edge (TLS terminates before
# traffic reaches this process), so the session/CSRF cookies are marked
# Secure there. Force it on explicitly once you've confirmed HTTPS is in
# front of you; leave off for local http://localhost testing.
DASHBOARD_FORCE_SECURE_COOKIES = os.getenv("DASHBOARD_FORCE_SECURE_COOKIES", "true" if os.getenv("RAILWAY_ENVIRONMENT") else "false").lower() in ("1", "true", "yes")

# --- Operational layer (does NOT affect strategy logic) -----------------
# All of these are ops/monitoring knobs only — signal generation itself is
# governed entirely by STRATEGY above and is untouched by any of this.
BOT_VERSION = os.getenv("BOT_VERSION", "1.0")
DB_PATH = os.getenv("DB_PATH", "data/bot_state.db")
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))
WATCHDOG_MINUTES = int(os.getenv("WATCHDOG_MINUTES", "20"))
TELEGRAM_MIN_GAP_SECONDS = float(os.getenv("TELEGRAM_MIN_GAP_SECONDS", "1.2"))

# Every backtest number in this repo (see analysis/PER_SYMBOL_CONFIG_NOTES.md)
# assumes at most one open position per symbol at a time — Backtester.run()
# defaults to max_open_trades=1 and never opens a new trade until the
# previous one has closed. Historically live_bot.py didn't enforce this at
# all: it sent every signal that cleared the strategy rules regardless of
# whether a prior signal on that symbol was still open, which is a
# meaningfully different (and in backtesting, notably riskier — deeper
# drawdowns from stacked concurrent risk) trading pattern than what was
# actually tested. This makes live match backtest: set to 1 to mirror the
# default backtest assumption exactly, or raise it if you deliberately want
# to test/run a concurrent-position variant (do that with eyes open — see
# the notes file for how much worse drawdown gets when this is uncapped).
LIVE_MAX_OPEN_PER_SYMBOL = int(os.getenv("LIVE_MAX_OPEN_PER_SYMBOL", "1"))

# A signal only actually exists once its LTF bar closes and the poll cycle
# picks it up — by then price may have already run partway toward TP1
# within that same bar, i.e. the "free" retracement the zone represents
# has partly already happened before you can act on the alert. This caps
# how much of the entry->TP1 distance the bar's close is allowed to have
# already covered before a signal is treated as stale/chased rather than
# sent as an actionable alert. 0.5 = if price closed more than halfway
# from entry to TP1 already, skip the live alert (still logged for
# visibility, status='stale_chased', same pattern as skipped_concurrency).
# Purely a live-alerting filter — never touches the backtester, which
# already assumes a perfect fill at `entry` regardless of this.
# Set to 1.0 (or higher) to disable.
STALE_SIGNAL_MAX_PROGRESS = float(os.getenv("STALE_SIGNAL_MAX_PROGRESS", "0.5"))

# Drawdown alerting (see smc_bot/drawdown.py). Tracks a simulated running
# balance per symbol using the exact same compounding formula as the
# backtester (balance * risk_percent/100 per R, applied per closed trade),
# so a live reading is directly comparable to a backtested max-drawdown
# figure. Fires once per threshold crossed since the last new equity high.
# Comma-separated percentages, ascending order recommended (not enforced).
DRAWDOWN_ALERT_THRESHOLDS_PCT = [
    float(x) for x in os.getenv("DRAWDOWN_ALERT_THRESHOLDS_PCT", "25,40,55").split(",") if x.strip()
]

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

# --- MT5 (Windows VPS execution layer) ----------------------------------
# Only used by mt5_live_bot.py, the Windows-only entrypoint that fetches
# data from and places demo orders in a running MT5 terminal. Not used by
# live_bot.py (Telegram-alerts-only, Railway/Linux) at all -- the two
# entrypoints are independent; see MT5_VPS_SETUP.md.
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0") or "0")
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
# Only needed if the terminal isn't the one MT5's Python package finds by
# default -- path to terminal64.exe, e.g.
# "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", "")
# Refuses to place orders unless the connected account is a demo account.
# Do not set this to false without understanding what you're doing.
MT5_DEMO_ONLY = os.getenv("MT5_DEMO_ONLY", "true").lower() in ("1", "true", "yes")
# Unique tag on every order this bot places, so you (and the bot, on
# restart) can tell its positions apart from manual trades on the same
# account.
MT5_MAGIC_NUMBER = int(os.getenv("MT5_MAGIC_NUMBER", "26071601"))
# Broker demo servers name instruments differently (XAUUSD vs XAUUSD.m vs
# GOLD, BTCUSD vs BTCUSDm, etc). List extra candidate names per symbol,
# comma-separated, tried in order after the SYMBOLS[...].name itself;
# mt5_feed.resolve_symbol() picks whichever one actually exists on your
# broker. Check your MT5 Market Watch for the exact spelling and set these
# explicitly rather than relying on the fuzzy-match fallback.
MT5_SYMBOL_CANDIDATES = {
    "XAUUSD": [c.strip() for c in os.getenv("MT5_XAUUSD_CANDIDATES", "XAUUSD,XAUUSD.m,XAUUSDm,GOLD,GOLD.m").split(",") if c.strip()],
    "BTCUSD": [c.strip() for c in os.getenv("MT5_BTCUSD_CANDIDATES", "BTCUSD,BTCUSD.m,BTCUSDm,BTCUSDT").split(",") if c.strip()],
}
MT5_POLL_SECONDS = int(os.getenv("MT5_POLL_SECONDS", "60"))


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
