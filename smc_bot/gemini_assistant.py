"""
Gemini-backed /ask assistant for Telegram.

Scope, deliberately narrow: this answers questions ABOUT the bot (its
signals, stats, uptime, logs) using the data already in the SQLite store.
It has no access to place trades, no access to change STRATEGY or any
config, and no ability to trigger bot actions — it's read-only Q&A over
data that already exists. The system prompt also explicitly tells Gemini
not to give financial advice or invent numbers, since a signal bot's
Telegram assistant is exactly the kind of thing someone could
misinterpret as trade advice if it free-associates.
"""
import base64
import json
import logging

import requests

from . import health

logger = logging.getLogger(__name__)

GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM_PREAMBLE = (
    "You are a read-only status assistant for a trading SIGNAL bot (it only "
    "sends alerts, it never places trades). Answer the user's question using "
    "ONLY the context data provided below. Do not give financial advice, do "
    "not recommend whether to take a trade, and do not invent any number, "
    "signal, or statistic that isn't present in the context. If the context "
    "doesn't contain what's needed to answer, say so plainly. Keep answers "
    "short and to the point, suitable for a Telegram message."
)


def build_context(store, symbols: list) -> str:
    perf = store.performance_stats()
    recent = store.recent_signals(10)
    recent_lines = [
        f"#{r['id']} {r['symbol']} {r['direction']} entry={r['entry']:.2f} "
        f"status={r['status']} sent_at={r['sent_at']}"
        for r in recent
    ] or ["(no signals logged yet)"]

    return (
        f"Markets tracked: {', '.join(symbols)}\n"
        f"Bot uptime: {health.uptime_str()}\n"
        f"Last scan: {health.last_scan_str()}\n"
        f"Markets scanned this run: {health.markets_scanned()}\n\n"
        f"Performance (closed signals only): {perf}\n\n"
        f"Last 10 signals:\n" + "\n".join(recent_lines)
    )


CHART_ANALYSIS_PROMPT = """You are analyzing a trading chart screenshot the user uploaded.
Look ONLY at what's visible in the image -- candlestick structure, trend,
and any price levels you can read or estimate from the axis. You have no
information beyond this single image.

IMPORTANT HONESTY RULE: you are reading price levels from pixel positions
in a static image, not from real numeric price data. Your entry/stop/
target numbers are estimates, not precise values. Do not present them
with false confidence. Grade critically -- do not grade generously.

Respond with STRICT JSON ONLY, no markdown fences, no extra text, in
exactly this shape:

{
  "symbol_guess": "<best guess at the instrument, or 'unknown'>",
  "direction": "long" | "short" | "no_trade",
  "grade": "A+" | "A" | "B+" | "B" | "C" | "F",
  "entry": <number or null>,
  "stop_loss": <number or null>,
  "take_profit_1": <number or null>,
  "take_profit_2": <number or null>,
  "risk_reward": <number or null>,
  "support_levels": [<numbers>],
  "resistance_levels": [<numbers>],
  "key_strengths": ["<short bullet>", ...],
  "key_concerns": ["<short bullet>", ...],
  "reasoning": "<2-3 sentence summary of the setup>",
  "invalidation": "<one sentence: what would prove this wrong>",
  "trailing_stop_note": "<one sentence suggestion on managing the stop as the trade develops, or null>",
  "alt_entry": "<one sentence alternate entry scenario, or null>"
}

If you can't identify a clean setup, use "no_trade" and grade "F" or "C",
still filling entry/stop/target with your best-guess reference levels
where possible."""


def analyze_chart_image(api_key: str, model: str, image_bytes: bytes, mime_type: str = "image/jpeg"):
    """
    Send a chart screenshot to Gemini vision and return (verdict_dict, raw_text).
    verdict_dict is None if the response couldn't be parsed as JSON --
    raw_text is always returned so the caller can log/inspect it either way.
    """
    if not api_key:
        return None, "Gemini isn't configured (missing GEMINI_API_KEY)."

    url = GEMINI_URL_TMPL.format(model=model)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": CHART_ANALYSIS_PROMPT},
                    {"inline_data": {"mime_type": mime_type, "data": b64}},
                ],
            }
        ]
    }

    try:
        resp = requests.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except requests.RequestException as e:
        logger.warning("Gemini vision request failed: %s", e)
        return None, f"Couldn't reach Gemini right now: {e}"
    except (KeyError, IndexError, TypeError):
        logger.error("Unexpected Gemini vision response shape: %s", locals().get("data"))
        return None, "Gemini returned something unexpected."

    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        if clean.lower().startswith("json"):
            clean = clean[4:]
        clean = clean.strip()

    try:
        verdict = json.loads(clean)
        return verdict, text
    except json.JSONDecodeError:
        logger.warning("Could not parse Gemini vision JSON: %s", text[:300])
        return None, text


def format_chart_analysis_for_telegram(verdict: dict) -> str:
    """Render a verdict dict as a readable Telegram message (plain text,
    since Telegram can't render the rich card UI a web dashboard could)."""
    lines = []
    grade = verdict.get("grade", "?")
    direction = (verdict.get("direction") or "unknown").upper()
    symbol = verdict.get("symbol_guess", "unknown")

    lines.append(f"📊 Chart Analysis -- {symbol}")
    lines.append(f"Grade: {grade}  |  Direction: {direction}")
    lines.append("")

    if verdict.get("entry") is not None:
        lines.append(f"Entry: {verdict['entry']}")
    if verdict.get("stop_loss") is not None:
        lines.append(f"Stop: {verdict['stop_loss']}")
    if verdict.get("take_profit_1") is not None:
        lines.append(f"TP1: {verdict['take_profit_1']}")
    if verdict.get("take_profit_2") is not None:
        lines.append(f"TP2: {verdict['take_profit_2']}")
    if verdict.get("risk_reward") is not None:
        lines.append(f"R/R: {verdict['risk_reward']}")

    if verdict.get("reasoning"):
        lines.append("")
        lines.append(f"Summary: {verdict['reasoning']}")

    strengths = verdict.get("key_strengths") or []
    if strengths:
        lines.append("")
        lines.append("Strengths:")
        for s in strengths:
            lines.append(f"  + {s}")

    concerns = verdict.get("key_concerns") or []
    if concerns:
        lines.append("")
        lines.append("Concerns:")
        for c in concerns:
            lines.append(f"  - {c}")

    if verdict.get("invalidation"):
        lines.append("")
        lines.append(f"Invalidation: {verdict['invalidation']}")

    if verdict.get("trailing_stop_note"):
        lines.append(f"Trailing stop: {verdict['trailing_stop_note']}")

    if verdict.get("alt_entry"):
        lines.append(f"Alt entry: {verdict['alt_entry']}")

    lines.append("")
    lines.append("⚠️ AI estimate from an image, not live price data. Not a substitute for your own analysis.")

    return "\n".join(lines)


def ask(api_key: str, model: str, question: str, context_text: str) -> str:
    if not api_key:
        return "Gemini isn't configured yet — add GEMINI_API_KEY in Railway to use /ask."

    url = GEMINI_URL_TMPL.format(model=model)
    prompt = f"{SYSTEM_PREAMBLE}\n\nContext:\n{context_text}\n\nQuestion: {question}"

    try:
        resp = requests.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except requests.RequestException as e:
        logger.warning("Gemini request failed: %s", e)
        return "Couldn't reach Gemini right now — try again shortly."
    except (KeyError, IndexError, TypeError):
        logger.error("Unexpected Gemini response shape: %s", locals().get("data"))
        return "Gemini returned something unexpected — check logs."
