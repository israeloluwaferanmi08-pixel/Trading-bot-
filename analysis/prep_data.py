import pandas as pd
import glob

files = sorted(glob.glob("rawdata/BTCUSDT-15m-*.csv"))
dfs = []
for f in files:
    df = pd.read_csv(f)
    dfs.append(df)
df = pd.concat(dfs, ignore_index=True)
df["time"] = pd.to_datetime(df["open_time"], unit="ms")
df = df[["time","open","high","low","close","volume"]].sort_values("time").drop_duplicates(subset="time").reset_index(drop=True)
print("M15 rows:", len(df), df["time"].min(), "->", df["time"].max())

# check for gaps
diffs = df["time"].diff().dropna()
expected = pd.Timedelta(minutes=15)
gaps = diffs[diffs != expected]
print("Gap count:", len(gaps))
if len(gaps):
    print(gaps.value_counts().head())

df.to_csv("data/btcusd_m15_real.csv", index=False)

# Build H4 by resampling
df_idx = df.set_index("time")
h4 = df_idx.resample("4h").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()
print("H4 rows:", len(h4), h4["time"].min(), "->", h4["time"].max())
h4.to_csv("data/btcusd_h4_real.csv", index=False)
