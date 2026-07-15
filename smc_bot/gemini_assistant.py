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
