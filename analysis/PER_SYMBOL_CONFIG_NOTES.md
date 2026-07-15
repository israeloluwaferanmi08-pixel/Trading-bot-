# Per-symbol strategy config (post BOS/CHoCH backtesting)

`config.py` now exposes `get_strategy_params(symbol)` instead of a single
global `STRATEGY` dict. `run_backtest.py` and `live_bot.py` both use it —
don't read `config.STRATEGY` directly if you're adding new call sites.

## Why symbols diverged

Backtesting BOS/CHoCH (LTF structure confirmation) and the two cooldowns
on real data showed they don't transfer across symbols:

- **XAUUSD** (15m, Feb-Apr 2026): BOS/CHoCH cut trade count roughly in
  half and made *every* metric worse, including drawdown (26.5% with it
  on vs the numbers below with it off). Rejected for this symbol.
- **BTCUSD** (15m, Dec 2024-Jun 2025): BOS/CHoCH raised win rate and
  profit factor and roughly halved drawdown, at the cost of fewer trades
  and lower raw return. We went with raw return over drawdown-adjusted
  return for BTC, so it's off here too — see the override comment in
  `config.py` if you want to flip that trade-off later.

## Current configs (both at 5% risk/trade)

| | XAUUSD | BTCUSD |
|---|---|---|
| require_structure_confirmation | False | False |
| loss cooldown (ATR-based) | on | on |
| consecutive-loss streak cooldown | **off** | on |
| Backtest trades | 51 | 168 |
| Win rate | 47.06% | 50.6% |
| Profit factor | 1.78 | 2.17 |
| Return | +145.5% | +6,758% |
| Max drawdown | 18.55% | 38.63% |

## Caveats worth remembering

- Both are single-sample backtests (3 months XAU, 7 months BTC) — treat
  as a working hypothesis, not a proven edge, until validated
  out-of-sample.
- 5% risk/trade is aggressive; it's what's driving the large return
  numbers as much as the underlying strategy edge is. Decide deliberately
  whether that's the risk level you'd run live.
- BTCUSD's 38.63% max drawdown is the number to have made peace with
  before a live losing streak hits it, not after.
- If you add a third symbol, add its override block in
  `STRATEGY_OVERRIDES` (or leave it out to inherit the BTC-tuned base)
  rather than assuming either existing symbol's config is a safe default.
