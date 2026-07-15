"""
Minimal Telegram notifier using the raw Bot API over HTTPS (no extra SDK
dependency needed beyond `requests`).

Setup:
  1. Talk to @BotFather on Telegram, /newbot, get your bot token.
  2. Send your bot any message, then visit:
       https://api.telegram.org/bot<TOKEN>/getUpdates
     to find your chat_id (or add the bot to a group/channel and use that
     chat's id).
  3. Put both values in your .env file (see .env.example).
"""
import logging
import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 10):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send_message(self, text: str, retries: int = 3) -> bool:
        if not self.enabled():
            logger.warning("Telegram not configured (missing token/chat_id) — message not sent:\n%s", text)
            return False

        url = f"{self.base_url}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text}

        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    return True
                logger.error("Telegram send failed (%s): %s", resp.status_code, resp.text)
            except requests.RequestException as e:
                logger.error("Telegram send exception (attempt %d/%d): %s", attempt, retries, e)
        return False
