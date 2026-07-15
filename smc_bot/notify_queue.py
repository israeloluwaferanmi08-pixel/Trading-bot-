"""
Wraps the existing TelegramNotifier with a background send queue so that
several signals firing in the same cycle go out one-by-one with a small
gap between them, instead of hammering the Bot API at once and risking a
429 rate-limit response. Signal-generation logic in signals.py is
untouched — this only affects how already-built messages are delivered.
"""
import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)


class QueuedNotifier:
    def __init__(self, notifier, min_gap_seconds: float = 1.2):
        self._notifier = notifier
        self._min_gap = min_gap_seconds
        self._q: "queue.Queue[str]" = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def enabled(self) -> bool:
        return self._notifier.enabled()

    def send_message(self, text: str) -> None:
        """Non-blocking: enqueue and return immediately."""
        self._q.put(text)

    def send_now(self, text: str) -> bool:
        """Blocking, bypasses the queue — used for startup/shutdown so the
        message goes out before the process exits."""
        return self._notifier.send_message(text)

    def _worker(self):
        last_sent = 0.0
        while True:
            text = self._q.get()
            wait = self._min_gap - (time.time() - last_sent)
            if wait > 0:
                time.sleep(wait)
            try:
                self._notifier.send_message(text)
            except Exception:
                logger.exception("QueuedNotifier: failed to send message")
            last_sent = time.time()
            self._q.task_done()
