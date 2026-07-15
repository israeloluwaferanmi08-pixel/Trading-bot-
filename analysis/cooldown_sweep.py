import pandas as pd
from smc_bot import config
from smc_bot.data_feed import load_csv
from smc_bot.backtester import Backtester
from smc_bot.metrics import summarize
import copy

df_ltf = load_csv("data/btcusd_m15_real.csv")
df_htf = load_csv("data/btcusd_h4_real.csv")
sym_cfg = config.SYMBOLS["BTCUSD"]

def run_with(atr_mult, bars):
    params = copy.deepcopy(config.STRATEGY)
    params["loss_cooldown_atr_mult"] = atr_mult
    params["loss_cooldown_bars"] = bars
    bt = Backtester("BTCUSD", params, config.BACKTEST, pip_size=sym_cfg.pip_size)
    result = bt.run(df_ltf, df_htf, max_open_trades=1)
    s = summarize(result)

    # April-only slice
    trades = [t for t in result["trades"] if t.closed]
    tdf = pd.DataFrame([{"close_time": t.close_time, "realized_r": t.realized_r} for t in trades])
    tdf["month"] = pd.to_datetime(tdf["close_time"]).dt.to_period("M")
    apr = tdf[tdf["month"]==pd.Period("2025-04")]
    apr_r = round(apr["realized_r"].sum(),2)
    apr_n = len(apr)
    apr_wr = round(100*(apr["realized_r"]>0).mean(),1) if apr_n else 0

    return dict(atr_mult=atr_mult, bars=bars, total_trades=s["total_trades"],
                win_rate=s["win_rate_pct"], total_r=s["total_r"], pf=s["profit_factor"],
                return_pct=s["return_pct"], max_dd=s["max_drawdown_pct"],
                apr_trades=apr_n, apr_r=apr_r, apr_wr=apr_wr)

configs = [
    (0, 0),        # disabled (baseline)
    (2.0, 48),     # current default
    (2.0, 96),
    (2.5, 96),
    (3.0, 96),
    (2.5, 192),
    (3.0, 192),
]

rows = [run_with(a,b) for a,b in configs]
res = pd.DataFrame(rows)
pd.set_option("display.width", 160)
print(res.to_string(index=False))
