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
        "realized_r": t.realized_r,
        "win": t.realized_r > 0,
    })
tdf = pd.DataFrame(rows)
tdf["month"] = pd.to_datetime(tdf["close_time"]).dt.to_period("M")

# Approx BTC price direction per month using htf close at month start/end for regime labeling
htf = df_htf.copy()
htf["month"] = htf["time"].dt.to_period("M")
price_by_month = htf.groupby("month")["close"].agg(["first","last"])
price_by_month["chg_pct"] = (price_by_month["last"] - price_by_month["first"]) / price_by_month["first"] * 100

summary = tdf.groupby("month").agg(
    trades=("realized_r","count"),
    wins=("win","sum"),
    total_r=("realized_r","sum"),
    avg_r=("realized_r","mean"),
    longs=("direction", lambda s: (s=="BUY").sum()),
    shorts=("direction", lambda s: (s=="SELL").sum()),
)
summary["win_rate_pct"] = (summary["wins"] / summary["trades"] * 100).round(1)
summary["total_r"] = summary["total_r"].round(2)
summary["avg_r"] = summary["avg_r"].round(3)
summary = summary.join(price_by_month["chg_pct"].round(1).rename("btc_chg_pct"))

print(summary.to_string())

import matplotlib.pyplot as plt
fig, ax1 = plt.subplots(figsize=(10,5))
months = [str(m) for m in summary.index]
ax1.bar(months, summary["total_r"], color=["#2ca02c" if v>=0 else "#d62728" for v in summary["total_r"]], alpha=0.8, label="Total R")
ax1.set_ylabel("Total R (strategy)")
ax1.axhline(0, color="gray", linewidth=0.8)
ax2 = ax1.twinx()
ax2.plot(months, summary["btc_chg_pct"], color="black", marker="o", label="BTC % change")
ax2.set_ylabel("BTC monthly % change")
ax1.set_title("BTCUSD SMC bot: monthly R vs BTC price change")
fig.tight_layout()
plt.savefig("monthly_breakdown.png", dpi=120)
print("saved chart")
