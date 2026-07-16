# SMC Bot — Supply/Demand + Premium/Discount signal bot (BTCUSD & XAUUSD)

A signal-generation bot for **XAUUSD** and **BTCUSD** that:

- Detects **supply & demand zones** from displacement (impulsive) price legs
- Filters zones by **premium/discount** position within the current dealing range
- Confirms with **higher-timeframe trend** (EMA-based)
- Sends signals to a **Telegram** chat/channel
- Comes with a **look-ahead-safe, walk-forward backtester** so you can test
  the strategy on your own historical data before risking anything live

> ⚠️ **This is not financial advice, and no bot can guarantee "accurate"
> signals.** Every strategy has losing streaks and drawdowns — that's why
> the backtester exists. Forward-test on a demo account before ever
> connecting this to real money, and never risk more than you can afford
> to lose. Past backtest performance does not guarantee future results.

---

## 1. Strategy logic (exactly what the bot trades)

**Supply/demand zone detection**
1. Find confirmed swing highs/lows (5-bar fractals by default).
2. A leg between two swings counts as an **impulse** if it travels at least
   `1.5x ATR` — that displacement is what leaves imbalance behind.
3. The zone is the **origin candle** right before that impulsive leg:
   - Demand zone = last down/base candle before an impulsive rally.
   - Supply zone = last up/base candle before an impulsive drop.
4. A zone is **mitigated** (removed) the first time price closes back
   through it, or **stales out** if untouched for 250 bars.

**Premium / Discount**
- Dealing range = highest high / lowest low over the last 100 bars.
- Equilibrium = midpoint. Above it = premium, below it = discount.

**Signal rules** (this replaces liquidity-sweep + BOS/CHoCH entirely — no
FVG confluence, per spec):
- **BUY** — price taps an unmitigated demand zone sitting in the **discount**
  half of the range, AND the H4 trend is bullish (EMA50 > EMA200, price above both).
- **SELL** — price taps an unmitigated supply zone sitting in the **premium**
  half of the range, AND the H4 trend is bearish.

Entry = zone edge, SL = beyond the zone + ATR buffer, TP1/TP2 = 2R/3R by
default (configurable). After TP1, the stop moves to breakeven.

All of this is tunable in `smc_bot/config.py` under `STRATEGY`.

---

## 2. Project layout

```
smc_bot/
  config.py            symbols, strategy params, telegram/env settings
  indicators.py         ATR, EMA, vectorized swing-point detector
  zones.py               supply/demand zone detection + premium/discount range
  trend.py                HTF trend filter
  signals.py             combines the above into BUY/SELL signals
  backtester.py           walk-forward, look-ahead-safe backtest engine
  metrics.py              win rate / profit factor / drawdown reporting
  data_feed.py            ccxt / CSV data loaders
  telegram_notifier.py    Telegram Bot API wrapper
  live_bot.py             polling loop tying it all together

run_backtest.py          CLI: backtest on your own CSV data
generate_sample_data.py  generates synthetic OHLCV so you can test the
                           pipeline end-to-end before you have real data
requirements.txt
.env.example
```

---

## 3. Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

**Getting a Telegram bot token & chat id**
1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser to find your
   `chat_id` (or add the bot to a group/channel and use that chat's id).

**Data source**:
- **ccxt** (BTCUSD): pulls BTC/USDT from Binance by default — a reasonable
  proxy for BTCUSD price action but not identical to any specific broker's
  quote.
- **TwelveData** (XAUUSD, and BTCUSD if ccxt fails): there's no
  crypto-exchange equivalent for gold, so XAUUSD is served by TwelveData's
  REST API. See the TwelveData section below for API key setup.

---

## 4. Backtesting (do this first)

Test the pipeline immediately with synthetic data (no real data needed):

```bash
python generate_sample_data.py
python run_backtest.py --symbol XAUUSD --ltf-csv data/xauusd_m15.csv --htf-csv data/xauusd_h4.csv --plot
python run_backtest.py --symbol BTCUSD --ltf-csv data/btcusd_m15.csv --htf-csv data/btcusd_h4.csv --plot
```

**The synthetic data is a random walk — it has no real market structure or
edge, so poor backtest results on it are expected and meaningless.** It
only proves the code runs correctly end-to-end (zone detection, premium/
discount filtering, trend alignment, trade management, Telegram formatting).

To backtest for real, export historical M15 and H4 candles for XAUUSD/
BTCUSD from your broker/TradingView as CSV (columns: time/date, open,
high, low, close, [volume]) and point `run_backtest.py` at those files
instead. Expect to spend real time here tuning `STRATEGY` params in
`config.py` (impulse threshold, swing fractal width, zone lookback, R
multiples) against your actual data before trusting any live signal.

---

## 5. Running the live bot

```bash
python -m smc_bot.live_bot
```

This polls every `POLL_SECONDS` (default 60s), fetches the latest M15/H4
candles for XAUUSD and BTCUSD, evaluates the signal engine, and pushes any
new signal to your Telegram chat. Already-alerted zones are tracked in
`sent_signals.json` so you don't get duplicate pings for the same zone.

Recommended: run this on a VPS or always-on machine, and start on a demo
account.

---

## 6. Tuning

Everything strategy-related lives in `STRATEGY` in `smc_bot/config.py`:

| Param | What it does |
|---|---|
| `swing_left` / `swing_right` | fractal width for swing-point confirmation |
| `min_impulse_atr` | how strong a leg must be (in ATR) to count as impulsive |
| `dealing_range_lookback` | bars used to build the premium/discount range |
| `max_zone_age_bars` | how long an untouched zone stays valid |
| `sl_buffer_atr` | stop-loss buffer beyond the zone edge |
| `tp_r_multiples` | take-profit targets in R multiples |
| `htf_ema_fast` / `htf_ema_slow` | HTF trend EMAs |

There is no single "correct" way to define a supply/demand zone — this
implementation is one reasonable, documented interpretation (see
`zones.py` docstring). If you have a specific zone definition from a
mentor/course you follow, it's worth comparing against this one and
adjusting `zones.py` accordingly.

---

## 7. Limitations & honest caveats

- No strategy — including this one — has a proven statistical edge just
  because it's "logically sound." Back- and forward-test thoroughly.
- The backtester models spread/slippage as a flat cost and assumes SL
  triggers before TP when both land in the same candle (conservative but
  approximate — real execution can differ, especially on gappy weekend
  crypto opens).
- ccxt's BTC/USDT is not identical to a broker's BTCUSD CFD quote.
- This bot **only generates and sends signals** — it does not place trades.
  Wiring it to actually execute orders is a separate, higher-stakes step
  (and one you should only take after extensive demo testing).

---

## Production / Railway deployment layer

This section covers the operational layer added on top of the strategy
above. **None of it changes signal logic, entries, stops, targets, or
`STRATEGY` in `config.py`.** It only makes the bot safe to run unattended
on Railway 24/7.

### ⚠️ Read this before deploying: data source on Railway

`data_feed.py` gets prices from **ccxt** for BTCUSD, falling back to
**TwelveData** for anything ccxt can't serve. The chain on Railway is:

**ccxt (BTCUSD only) → TwelveData**

- **BTCUSD**: resolved via ccxt/Binance, free and unmetered — no key needed,
  and it **never touches your TwelveData credits**, since ccxt is tried
  before TwelveData in the fallback chain.
- **XAUUSD**: has no crypto-exchange equivalent, so it skips straight past
  the ccxt step and is resolved via **TwelveData** (`twelvedata_feed.py`),
  using up to 3 free-tier API keys with automatic rotation — see below.

Only XAUUSD spends TwelveData credits, so your whole free-tier budget goes
toward the one symbol that actually needs it.

### TwelveData setup (free tier, 3-key rotation)

TwelveData's free ("Basic") plan is capped at **800 credits/day and 8
requests/minute per key**. Rather than paying for a higher tier,
`twelvedata_feed.py` rotates across up to 3 free keys: if one gets
rate-limited (HTTP 429, or a `"code":429` error in the response body),
that key goes on a cooldown timer (`TWELVEDATA_COOLDOWN_SECONDS`, default
60s) and the next key is tried automatically. This is pure data-fetching
plumbing — it doesn't touch signal logic.

Only **XAUUSD** spends these credits (BTCUSD is routed through ccxt
instead — see above), and each poll cycle fetches two timeframes (M15 and
H4) for it, so one poll = 2 credits.

1. Sign up free at https://twelvedata.com — **one account/key per free
   signup**, so create up to 3 separate accounts (e.g. 3 different email
   addresses) if you want the full 3-key rotation.
2. Put the keys in `TWELVEDATA_API_KEY_1/2/3` (Railway → Variables, or
   your local `.env`). Only key 1 is required — 2 and 3 are extra headroom.
3. `POLL_SECONDS` defaults to `90` in `.env.example`. At 90s that's 960
   polls/day × 2 credits = **1,920 credits/day** — comfortably inside the
   combined **2,400/day** budget of a full 3-key rotation (≈80%
   utilization), but well over a single key's 800/day cap. If you're
   running with only 1 or 2 keys, slow down accordingly: roughly `216s+`
   for one key, `108s+` for two, to stay under budget. The 8/min-per-key
   cap is never the binding constraint here — 2 credits every 90s is
   nowhere near 8/min even on a single key.
4. If all configured keys are cooling down at once, that cycle's data
   fetch fails and you'll get the normal "Data feed (XAUUSD) error" alert
   — it'll pick back up next cycle once a cooldown clears.

### Gemini `/ask` assistant

Message your bot `/ask <question>` (e.g. `/ask how many signals today?`
or `/ask what's my win rate this week?`) and Gemini answers using only
the bot's own logged data (`gemini_assistant.py` builds the context from
the SQLite store — uptime, last scan, performance stats, last 10 signals).

This is deliberately **read-only and advisory-free**: the system prompt
tells Gemini not to give financial advice, not to recommend taking a
signal, and not to invent any number that isn't in the context. It can't
place trades, change settings, or influence signal generation — it only
answers questions about data that already exists.

Get a free key at https://aistudio.google.com/apikey and set
`GEMINI_API_KEY` (and optionally `GEMINI_MODEL`, defaults to
`gemini-2.5-flash`). Without a key, `/ask` replies telling you it's not
configured rather than failing silently.

### What's new

| Feature | Where |
|---|---|
| Health monitoring + hourly heartbeat | `health.py`, `alerts.py` |
| Error notifications to Telegram | `alerts.error_message`, wired into `live_bot.py` |
| Auto-restart / crash recovery | per-symbol `try/except` in `run_once`, `railway.json` restart policy |
| Duplicate signal protection | unchanged — still the existing zone-id dedupe in `live_bot.py` |
| Cooldown after a loss | unchanged — this was already in `STRATEGY["loss_cooldown_*"]` |
| Structured logging (SQLite) | `store.py` |
| Signal IDs | auto-increment `id` in the `signals` table, shown in every message |
| Daily / weekly reports | `reports.py`, scheduled from the main loop |
| Performance analytics (win rate, avg R, best/worst symbol & session) | `store.performance_stats()`, `/stats` |
| Telegram commands | `commands.py`: `/status /stats /signals /logs /startscan /stopscan /scannow /enable /disable /setinterval /reload /restart /ask /help` |
| Config reload without redeploy | `/setinterval`, `/reload` — operational knobs only, **not** strategy params |
| Watchdog | `watchdog.py` — hard-restarts the process if no scan completes for `WATCHDOG_MINUTES` |
| API retry/failover | `fetch_symbol_data()` in `live_bot.py`: ccxt → TwelveData, 3 retries per source |
| TwelveData multi-key rotation | `twelvedata_feed.py` — rotates across up to 3 free keys on rate limit |
| Gemini `/ask` assistant | `gemini_assistant.py` — read-only Q&A over bot stats/signals, no trading influence |
| Telegram message queue | `notify_queue.py` — spaces out bursts of signals so Telegram doesn't rate-limit you |
| Startup / shutdown messages | `alerts.startup_message` / `shutdown_message` |
| Signal confluence breakdown | `session.py: confluence_for()` — see note below |
| Trade outcome tracking (TP/SL/expired) | `outcomes.py`, runs each cycle against the candles already fetched |
| **Web admin dashboard** | `dashboard.py` + `templates/` — status, controls, performance, signals/errors, equity chart. See below. |

### Web dashboard

A token-gated web UI runs in a background thread inside the same process
as the live bot loop (no second service, shares the same SQLite `Store`).
It's read-only except for the same operational knobs already exposed over
Telegram — scan on/off, scan now, per-symbol enable/disable, poll
interval. **Nothing on the dashboard can touch `STRATEGY` or how a signal
is generated.**

**What it shows:** scan status, uptime, memory/CPU, last-scan time,
restart count · overall + per-symbol win rate/avg R/best-worst breakdown ·
a cumulative-R equity chart over closed trades · the last 30 signals with
outcome · the last 20 logged errors.

**What it controls:** pause/resume scanning, force an immediate scan
cycle, enable/disable a specific market, change the poll interval live —
each of these is the same `BotState` object the Telegram `/commands` use,
so a change from one shows up in the other instantly.

**Setup**
1. `DASHBOARD_ENABLED` defaults to `true`. Set to `false` to disable it
   entirely (e.g. if you don't want any HTTP surface at all).
2. Set `DASHBOARD_TOKEN` in your environment to a long random string —
   without it, a random token is generated each process start and sent to
   your Telegram admin chat (and logged), so you're never locked out, but
   the URL you need changes on every restart unless you set it yourself.
3. On Railway: the `Procfile`/`railway.json` process is named `web` and
   binds to `$PORT` automatically, but Railway only issues a public URL
   once you turn on **networking** for the service (Settings → Networking
   → Generate Domain, or attach a custom domain). Local runs default to
   port 8080 (`DASHBOARD_PORT` to override).
4. Open `https://<your-railway-domain>/?token=<your token>` — the token
   is checked once and then kept in a session cookie, so you don't need
   to keep it in the URL after the first load.
5. Uses `waitress` as the WSGI server if installed (it's in
   `requirements.txt`); falls back to Flask's built-in dev server
   otherwise, which is fine for a single-operator dashboard but not
   something to expose at real traffic volume.

**Honest limitations:** single shared token, not per-user accounts — this
is a personal ops panel, not a multi-tenant product surface. If you're
planning to eventually offer this as a service to other people, the auth
model here would need to change (see the plugin-architecture /
multi-tenant discussion from the roadmap).

**Note on "confidence": your strategy is a hard AND-filter (zone kind +
premium/discount position + HTF trend all have to align), not a scored
model — so there's no real confidence percentage to show. Rather than
invent one, each signal message shows the actual conditions that fired
it (a confluence checklist), which is honest about what the strategy
actually checked.**

### Setup

1. Copy `.env.example` to `.env` and fill in your values — **never commit
   `.env`** (it's in `.gitignore`). On Railway, set these as environment
   variables in the service's Variables tab instead of a file.
2. `ADMIN_CHAT_ID` controls who can send bot commands — defaults to your
   `TELEGRAM_CHAT_ID`.
3. Attach a **Railway Volume** and set `DB_PATH` (and `STATE_FILE`) to a
   path inside it, e.g. `/data/bot_state.db`. Without a Volume, Railway's
   filesystem is wiped on every redeploy, so your signal history, IDs, and
   dedupe state reset each time you push.
4. Push to Railway. `Procfile` / `railway.json` are already set up
   (`restartPolicyType: ALWAYS`), so crashes, `/restart`, and watchdog
   exits all get relaunched automatically.
5. Message your bot `/help` once it's running to see all commands.

### What I deliberately left alone

- `STRATEGY` in `config.py` — zero changes.
- `signals.py`, `zones.py`, `trend.py`, `indicators.py`, `backtester.py` —
  zero changes.
- The extra time-based "cooldown after a signal" some checklists suggest
  (e.g. "don't send another BUY for 15 min") — this would silence some
  signals your strategy would otherwise send, which is a behavior change,
  not just an ops improvement. I didn't add it. If you want it, it's a
  small addition, but say so explicitly since it does affect what you
  get notified about.
