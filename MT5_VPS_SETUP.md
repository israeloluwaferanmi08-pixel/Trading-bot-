# Running the bot fully automated on MT5 (demo) via a Windows VPS

This sets up `mt5_live_bot.py`: it fetches prices straight from your MT5
terminal, evaluates the exact same `SignalEngine`/`STRATEGY` as the
backtester, and **places and manages real orders in your MT5 demo account**
with no manual step. It also still sends you Telegram alerts, same as
`live_bot.py`.

**This is separate from the Railway/Linux deployment (`live_bot.py`).**
Don't run both against the same symbols at once, or you'll get duplicate
Telegram alerts (and, worse, `live_bot.py` doesn't execute anything, so it
won't conflict on orders — but the alert duplication is still annoying).
Pick one:
- Alerts-only, cheap always-on hosting → keep `live_bot.py` on Railway.
- Fully automated demo execution → this guide, `mt5_live_bot.py`, Windows VPS.

---

## Why Windows specifically

The official `MetaTrader5` Python package only works on Windows, and only
alongside an actual running MT5 terminal process on that same machine —
there's no headless/Linux MT5 API. This is a MetaQuotes limitation, not
something this codebase can work around. (Wine-based Linux MT5 setups
exist in the wild but are unsupported and fragile — not recommended for
something you want running unattended 24/7.)

---

## 1. Get a Windows VPS

Any of these work — pick based on price/location/what you already use:

- A broker-provided VPS (some brokers offer a free VPS if you trade with
  them — check your broker's site, since this is often the cheapest option
  and comes with MT5 pre-friendly network latency to their servers)
- Generic cloud Windows VPS: Vultr, Contabo, Hetzner, AWS Lightsail (all
  offer "Windows Server" images)

Minimum spec is modest — this bot is not compute-heavy (2 symbols, one
poll per minute, no ML inference): 1-2 vCPU, 2-4GB RAM, any small SSD tier
is plenty. Pick a region close to your broker's server for lower latency
if you're picky about slippage, though for a demo account this doesn't
matter much.

Once provisioned, connect via Remote Desktop (RDP) — the VPS provider
gives you an IP, username, and password for this.

---

## 2. Install MT5 and open a demo account

1. On the VPS, download and install the MT5 terminal from your broker's
   site (or the generic https://www.metatrader5.com/en/download if you
   just want any demo, e.g. via the "MetaQuotes-Demo" server that ships
   with a fresh install).
2. Open MT5 → **File → Open an Account** → choose "Open a demo account" →
   pick a broker/server → fill in your details → note down:
   - **Login** (a number)
   - **Password**
   - **Server** (e.g. `MetaQuotes-Demo` or `YourBroker-Demo`)
3. **Enable algo trading**: Tools → Options → Expert Advisors tab → check
   "Allow algorithmic trading" (also toggle the "Algo Trading" button in
   the main toolbar so it's green, not red).
4. In Market Watch (Ctrl+M), right-click → "Show All" so XAUUSD/BTCUSD (or
   whatever your broker calls them) are visible — note the **exact
   spelling**, since brokers vary (`XAUUSD` vs `XAUUSD.m` vs `GOLD`,
   `BTCUSD` vs `BTCUSDm`). You'll need this for `MT5_XAUUSD_CANDIDATES` /
   `MT5_BTCUSD_CANDIDATES` below if the bot's fuzzy-match doesn't find it
   automatically (it logs loudly if it has to guess).
5. Leave the terminal open and logged in to this demo account — it needs
   to keep running in the background for the Python bridge to talk to it.

---

## 3. Install Python and the bot on the VPS

1. Install Python 3.11+ from https://python.org (check "Add to PATH"
   during install).
2. Copy this project folder to the VPS (RDP file transfer, or `git clone`
   if it's in a repo, or zip + upload).
3. Open a Command Prompt in the project folder:
   ```
   pip install -r requirements.txt
   pip install -r requirements-mt5.txt
   ```
4. Create a `.env` file in the project root (same folder as
   `mt5_live_bot.py`):
   ```
   MT5_LOGIN=12345678
   MT5_PASSWORD=your-demo-password
   MT5_SERVER=YourBroker-Demo
   MT5_DEMO_ONLY=true

   TELEGRAM_BOT_TOKEN=your-token
   TELEGRAM_CHAT_ID=your-chat-id

   # only needed if the auto-detected symbol name is wrong -- check step 2.4
   MT5_XAUUSD_CANDIDATES=XAUUSD,XAUUSD.m,GOLD
   MT5_BTCUSD_CANDIDATES=BTCUSD,BTCUSDm
   ```
5. Test it manually first:
   ```
   python mt5_live_bot.py
   ```
   Watch the console output — it should log connecting to MT5, resolving
   symbol names, and starting the poll loop. Leave it running for one full
   poll cycle (default 60s) and confirm no errors, then Ctrl+C to stop.

---

## 4. `MT5_DEMO_ONLY` — read this

`mt5_executor.py` checks the connected account's `trade_mode` before
placing any order and **refuses to trade if it isn't a demo account**.
This is intentional. Don't set `MT5_DEMO_ONLY=false` unless you fully
understand what you're doing — nothing about this codebase's backtest
results is a promise about live performance (see the caveats already
covered in this conversation: real spread/execution can differ from the
backtest's flat cost model, past performance isn't predictive, etc).

---

## 5. Keep it running 24/7

A Command Prompt window closes if you log out of RDP. To keep the bot (and
MT5 itself) running unattended:

**Option A — Task Scheduler (simplest)**
1. Search "Task Scheduler" → Create Task.
2. General tab: check "Run whether user is logged on or not".
3. Triggers tab: New → "At startup".
4. Actions tab: New → Program: `python`, Arguments:
   `C:\path\to\mt5_live_bot.py`, Start in: `C:\path\to\project`.
5. Settings tab: uncheck "Stop the task if it runs longer than", since
   this needs to run indefinitely.

Do the same for MT5 itself (Task Scheduler entry that launches
`terminal64.exe` at startup, logged into the demo account — MT5 remembers
the last login by default) so both come back up automatically if the VPS
reboots.

**Option B — NSSM (runs it as an actual Windows service)**
[NSSM](https://nssm.cc/) wraps any exe as a proper Windows service with
auto-restart on crash, which is more robust than Task Scheduler for a
process you want to survive crashes, not just reboots:
```
nssm install SMCBotMT5 "C:\Python311\python.exe" "C:\path\to\mt5_live_bot.py"
nssm set SMCBotMT5 AppDirectory "C:\path\to\project"
nssm start SMCBotMT5
```

Either way, also leave RDP's "disconnect" (not "sign out") behavior as the
norm when you close your remote session, so the desktop session (and MT5
GUI) stays alive in the background — signing out fully can close GUI apps
depending on VPS config.

---

## 6. What actually happens on a signal

Same signal generation as the backtester (`SignalEngine`, unchanged), but
now:

1. A new signal fires → logged to the SQLite store (same as `live_bot.py`)
   → Telegram alert sent.
2. Position size computed from `risk_percent` (config.py, per-symbol) of
   your **current MT5 demo account balance**, converted to lots using the
   broker's actual tick value/tick size for that symbol.
3. Two MT5 orders placed (half size each) — one closes at TP1, one at TP2
   — since MT5 doesn't have a native "partial take-profit" order type.
4. When the TP1-sized order closes (hit or stopped), the remaining order's
   SL is moved to breakeven automatically.
5. From then on, the remaining position's SL trails behind the most
   recently confirmed swing point each poll cycle (same idea as the
   backtester's `trail_stop_after_tp1`, but resolution-limited to your
   poll interval rather than every bar).

**Honest limitation**: because this now uses your broker's actual live
price feed (not the ccxt/TwelveData feed the backtester used), the exact
zones/entries it fires on **will not be identical** to the backtest run
earlier in this conversation — different broker, different quotes, same
strategy logic. Forward-test for a real stretch of time on demo before
treating the backtest numbers as a live performance expectation.

---

## 7. Sanity checklist before you walk away from it

- [ ] Algo trading toggle is green in MT5
- [ ] `.env` has the right demo login/password/server
- [ ] Symbol candidates resolve to the correct broker names (check the
      startup log line: `Resolved XAUUSD -> broker symbol 'XXXXXX'`)
- [ ] Telegram alerts are arriving (confirms both Telegram AND MT5 config
      are correct, since the startup message sends immediately)
- [ ] Task Scheduler / NSSM entry survives a manual VPS reboot test
- [ ] `MT5_DEMO_ONLY=true` — verified this hasn't been accidentally changed
