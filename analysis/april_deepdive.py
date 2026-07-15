import pandas as pd
from smc_bot import config
from smc_bot.data_feed import load_csv
from smc_bot.backtester import Backtester

df_ltf = load_csv("data/btcusd_m15_real.csv")
df_htf = load_csv("data/btcusd_h4_real.csv")

sym_cfg = config.SYMBOLS["BTCUSD"]
bt = Backtester("BTCUSD", config.get_strategy_params("BTCUSD"), config.BACKTEST, pip_size=sym_cfg.pip_size)
result = bt.run(df_ltf, df_htf, max_open_trades=1)

trades = [t for t in result["trades"] if t.closed]
rows = []
for t in trades:
    rows.append({
        "open_time": t.open_time,
        "close_time": t.close_time,
        "direction": t.direction,
        "entry": round(t.entry,1),
        "sl": round(t.stop_loss,1),
        "realized_r": round(t.realized_r,3),
    })
tdf = pd.DataFrame(rows)
tdf["month"] = pd.to_datetime(tdf["close_time"]).dt.to_period("M")
apr = tdf[tdf["month"]==pd.Period("2025-04")].sort_values("open_time")
pd.set_option("display.max_rows", None)
pd.set_option("display.width", 140)
print(apr[["open_time","close_time","direction","entry","sl","realized_r"]].to_string(index=False))

print()
print("Losers:", (apr["realized_r"]<=0).sum(), "/", len(apr))
print("Big losers (<= -0.9R):", (apr["realized_r"]<=-0.9).sum())
print("Small losers (-0.9R < r <= 0):", ((apr["realized_r"]>-0.9)&(apr["realized_r"]<=0)).sum())
print("Sum of loss R:", apr[apr["realized_r"]<=0]["realized_r"].sum())
print("Sum of win R:", apr[apr["realized_r"]>0]["realized_r"].sum())

# direction flips: how often does direction change trade to trade
flips = (apr["direction"] != apr["direction"].shift()).sum() - 1
print("Direction flips across the month:", flips, "out of", len(apr)-1, "consecutive pairs")
