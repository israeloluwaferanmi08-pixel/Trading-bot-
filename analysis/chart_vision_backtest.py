"""
Backtest for the "screenshot -> Gemini vision -> trade grade" idea.

WHAT THIS DOES
---------------
For N sample points spread across a historical CSV:
  1. Render a candlestick chart image of the PRECEDING window only
     (Gemini never sees what happens next -- no lookahead).
  2. Send that image to Gemini, asking for a structured trade read:
     direction, entry, stop, target, grade, confidence.
  3. Walk FORWARD through the real historical data that follows and
     check whether price hit the stop or the target first.
  4. Tally results: how often the direction call was right, how often
     target was hit before stop, and whether "grade" correlates with
     actual outcome.

IMPORTANT CAVEAT -- read before trusting any output number
-------------------------------------------------------------
If you're running this against the synthetic sample CSVs that ship
with this repo (data/*.csv), the results are MEANINGLESS -- that data
is a random walk with no real market structure, so there's nothing
real for Gemini to correctly read. Use REAL historical M15/H4 candles
(exported from MT5 or TradingView) for a result that means anything.

Also: this only tests Gemini's ability to read a rendered-from-data
chart image. It does not perfectly predict how well it'll read an
actual phone-screenshot upload (different resolution, watermarks,
whatever platform UI is visible), though it's a reasonable proxy.

SETUP
-----
    pip install pandas mplfinance requests

    export GEMINI_API_KEY="your key here"      # never hardcode this
    export GEMINI_MODEL="gemini-2.5-flash"       # or your preferred vision-capable model

USAGE
-----
    python chart_vision_backtest.py --csv ../data/xauusd_h4.csv \
        --samples 30 --window 100 --lookahead 40 --outdir ./vision_bt_out
"""
import argparse
import base64
import json
import os
import random
import sys
import time
from pathlib import Path

import pandas as pd
import requests

try:
    import mplfinance as mpf
except ImportError:
    print("Missing dependency. Run: pip install mplfinance", file=sys.stderr)
    raise

GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM_PROMPT = """You are grading a trading chart screenshot, SnapPChart-style.
Look ONLY at the candlestick structure, trend, and visible price levels in
the image. You do not have any information beyond what's in the image.

Respond with STRICT JSON ONLY, no markdown fences, no extra text, in
exactly this shape:

{
  "direction": "long" | "short" | "no_trade",
  "grade": "A+" | "A" | "B+" | "B" | "C" | "F",
  "entry": <number>,
  "stop_loss": <number>,
  "take_profit": <number>,
  "risk_reward": <number>,
  "reasoning": "<one or two sentences>",
  "invalidation": "<one sentence: what would prove this wrong>"
}

If you can't identify a clean setup, use "no_trade" and grade "F" or "C",
still filling entry/stop/target with your best-guess reference levels.
Be honest and critical -- do not grade generously. Use the exact price
values you can read or estimate from the chart's y-axis."""


def render_chart(df: pd.DataFrame, out_path: Path) -> None:
    """Render a window of OHLC data as a candlestick PNG."""
    plot_df = df.copy()
    plot_df.index = pd.to_datetime(plot_df["time"])
    plot_df = plot_df[["open", "high", "low", "close"]]
    mpf.plot(
        plot_df,
        type="candle",
        style="charles",
        volume=False,
        savefig=dict(fname=str(out_path), dpi=120, bbox_inches="tight"),
    )


def call_gemini_vision(api_key: str, model: str, image_path: Path) -> dict:
    image_bytes = image_path.read_bytes()
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    url = GEMINI_URL_TMPL.format(model=model)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": SYSTEM_PROMPT},
                    {"inline_data": {"mime_type": "image/png", "data": b64}},
                ],
            }
        ]
    }

    resp = requests.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

    # Strip markdown fences if the model added them despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_parse_error": True, "_raw_text": text}


def check_outcome(future_df: pd.DataFrame, direction: str, entry: float,
                   stop: float, target: float) -> str:
    """
    Walk forward bar by bar. Return one of:
    'target_hit', 'stop_hit', 'neither_hit', 'invalid_levels'
    """
    if direction == "no_trade":
        return "no_trade"
    if stop is None or target is None or entry is None:
        return "invalid_levels"

    for _, bar in future_df.iterrows():
        high, low = bar["high"], bar["low"]
        if direction == "long":
            if low <= stop:
                return "stop_hit"
            if high >= target:
                return "target_hit"
        elif direction == "short":
            if high >= stop:
                return "stop_hit"
            if low <= target:
                return "target_hit"
    return "neither_hit"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Path to historical OHLC CSV")
    ap.add_argument("--samples", type=int, default=30, help="Number of test windows")
    ap.add_argument("--window", type=int, default=100, help="Bars of history shown per chart")
    ap.add_argument("--lookahead", type=int, default=40, help="Bars to check forward for outcome")
    ap.add_argument("--outdir", default="./vision_bt_out", help="Where to save images + results")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Set GEMINI_API_KEY as an environment variable first.", file=sys.stderr)
        sys.exit(1)
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    df = pd.read_csv(args.csv)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    random.seed(args.seed)
    min_start = args.window
    max_start = len(df) - args.lookahead - 1
    if max_start <= min_start:
        print("Not enough rows in this CSV for the requested window/lookahead.", file=sys.stderr)
        sys.exit(1)

    sample_points = sorted(random.sample(range(min_start, max_start), args.samples))

    results = []
    for i, cutoff in enumerate(sample_points, 1):
        history = df.iloc[cutoff - args.window: cutoff]
        future = df.iloc[cutoff: cutoff + args.lookahead]

        img_path = outdir / f"sample_{i:03d}.png"
        render_chart(history, img_path)

        print(f"[{i}/{len(sample_points)}] cutoff row {cutoff} -> asking Gemini...")
        try:
            verdict = call_gemini_vision(api_key, model, img_path)
        except requests.RequestException as e:
            print(f"  Gemini call failed: {e}")
            results.append({"sample": i, "cutoff_row": cutoff, "error": str(e)})
            time.sleep(2)
            continue

        if verdict.get("_parse_error"):
            print(f"  Could not parse JSON response, raw text saved.")
            results.append({"sample": i, "cutoff_row": cutoff, "parse_error": True,
                             "raw_text": verdict["_raw_text"]})
            continue

        outcome = check_outcome(
            future, verdict.get("direction"),
            verdict.get("entry"), verdict.get("stop_loss"), verdict.get("take_profit"),
        )

        row = {
            "sample": i,
            "cutoff_row": cutoff,
            "direction": verdict.get("direction"),
            "grade": verdict.get("grade"),
            "entry": verdict.get("entry"),
            "stop_loss": verdict.get("stop_loss"),
            "take_profit": verdict.get("take_profit"),
            "risk_reward": verdict.get("risk_reward"),
            "outcome": outcome,
            "reasoning": verdict.get("reasoning"),
        }
        results.append(row)
        print(f"  {row['direction']} | grade {row['grade']} | outcome: {outcome}")

        time.sleep(1)  # be polite to the API, avoid rate limits

    results_df = pd.DataFrame(results)
    results_path = outdir / "results.csv"
    results_df.to_csv(results_path, index=False)

    # Summary
    valid = results_df[results_df["outcome"].isin(["target_hit", "stop_hit", "neither_hit"])]
    if len(valid) > 0:
        target_rate = (valid["outcome"] == "target_hit").mean()
        stop_rate = (valid["outcome"] == "stop_hit").mean()
        neither_rate = (valid["outcome"] == "neither_hit").mean()
        print("\n=== SUMMARY ===")
        print(f"Valid trade calls: {len(valid)} / {len(results_df)}")
        print(f"Target hit first: {target_rate:.0%}")
        print(f"Stop hit first:   {stop_rate:.0%}")
        print(f"Neither hit within lookahead: {neither_rate:.0%}")
        if "grade" in valid.columns:
            print("\nOutcome by grade:")
            print(valid.groupby("grade")["outcome"].value_counts())
    else:
        print("\nNo valid trade calls to summarize (check for parse errors / no_trade calls).")

    print(f"\nFull results saved to: {results_path}")
    print(f"Chart images saved to: {outdir}")


if __name__ == "__main__":
    main()
