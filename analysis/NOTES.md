# What's in this update

Original bot code is unchanged — `smc_bot/config.py` still has the
default `loss_cooldown_atr_mult=2.0, loss_cooldown_bars=48`.

## New real data (in `data/`)
- `btcusd_m15_real.csv` / `btcusd_h4_real.csv` — Binance BTCUSDT,
  Dec 2024 – Jun 2025 (7 months, gapless).
- `btcusd_m15_full2017_2022.csv` / `btcusd_h4_full2017_2022.csv` —
  Binance BTCUSDT, Aug 2017 – Nov 2022 (5+ years, 32 minor gaps).

Run e.g.:
```bash
python run_backtest.py --symbol BTCUSD --ltf-csv data/btcusd_m15_real.csv --htf-csv data/btcusd_h4_real.csv --plot
```

## Analysis scripts (in `analysis/`)
- `prep_data.py` / `prep_data2.py` — combine monthly Binance kline CSVs
  into the M15/H4 format `load_csv` expects.
- `monthly_breakdown.py` — groups one walk-forward run's trades by
  month (with BTC's monthly % move) to see where the edge concentrates.
- `april_deepdive.py` — per-trade dump for a single month.
- `cooldown_sweep.py` — sweeps `loss_cooldown_atr_mult` /
  `loss_cooldown_bars` and reports full-period + single-month metrics.

## Key findings so far (Dec 2024 – Jun 2025 sample)
- Default config: 187 trades, 49.2% win rate, PF 2.05, +160.7% return,
  9.63% max drawdown.
- Edge concentrated in trending/bearish months (Feb–Mar); April (a
  sharp V-shaped reversal) was the one losing month — many small -1R
  stop-outs from the zone detector re-entering the same band, not big
  blowups.
- Cooldown sweep suggests `loss_cooldown_atr_mult=3.0,
  loss_cooldown_bars=192` improves profit factor (2.50) and drawdown
  (6.42%) on this sample, but this was chosen after seeing the same
  data it's evaluated on — treat as a hypothesis to validate
  out-of-sample, not a confirmed better setting. Full 2017–2022
  history is included in this zip to run that validation.
