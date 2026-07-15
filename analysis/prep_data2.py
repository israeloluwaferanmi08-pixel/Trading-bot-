import pandas as pd

df = pd.read_csv("rawdata2/15m_BTCUSDT.csv")
df.columns = [c.strip().lower() for c in df.columns]
df["time"] = pd.to_datetime(df["time"])
df = df[["time","open","high","low","close","volume"]].sort_values("time").drop_duplicates(subset="time").reset_index(drop=True)
print("M15 rows:", len(df), df["time"].min(), "->", df["time"].max())

diffs = df["time"].diff().dropna()
expected = pd.Timedelta(minutes=15)
gaps = diffs[diffs != expected]
print("Gap count:", len(gaps))
if len(gaps):
    print(gaps.describe())
    print(gaps.sort_values(ascending=False).head(10))

df.to_csv("data/btcusd_m15_full2017_2022.csv", index=False)

df_idx = df.set_index("time")
h4 = df_idx.resample("4h").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()
print("H4 rows:", len(h4))
h4.to_csv("data/btcusd_h4_full2017_2022.csv", index=False)
