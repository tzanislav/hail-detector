"""Telegram bot commands: let anyone subscribe to the alerts by sending "sub".

Runs as its own service (hail-detector-bot) so a bot problem can never stop
radar polling. It long-polls Telegram's getUpdates, which needs no public
HTTPS endpoint, and only adds or removes subscribers; the poller does the
alert delivery to every subscriber.

Replies are in Bulgarian. Commands are matched case-insensitively, with or
without a leading slash or a trailing @BotName (as Telegram adds in groups).
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import Any

import httpx

from .archive import ArchiveStore
from .config import Settings
from .telegram import TelegramDeliveryError, TelegramNotifier


LOGGER = logging.getLogger(__name__)
TELEGRAM_API_URL = "https://api.telegram.org"
LONG_POLL_SECONDS = 50
ERROR_BACKOFF_SECONDS = 10

SUBSCRIBE_COMMANDS = frozenset(
    {"sub", "subscribe", "абонирай", "абонирай ме", "абонамент", "абониране"}
)
UNSUBSCRIBE_COMMANDS = frozenset(
    {"unsub", "unsubscribe", "stop", "отпиши", "отпиши ме", "отписване", "стоп"}
)
STATUS_COMMANDS = frozenset({"status", "статус"})

WHAT_YOU_GET = (
    "Ще получавате:\n"
    "• радарни сигнали за градушка близо до София;\n"
    "• официалните оранжеви и червени предупреждения на МетеоАларм за София град.\n\n"
    "Радарните сигнали са автоматични и НЕ са официално предупреждение."
)

WELCOME = (
    "✅ Абонирахте се за предупрежденията на HailBot.\n\n"
    f"{WHAT_YOU_GET}\n\n"
    "За да се отпишете, изпратете „unsub“ (или /stop) по всяко време."
)
ALREADY_SUBSCRIBED = (
    "ℹ️ Вече сте абонирани и ще получавате предупрежденията.\n\n"
    "За да се отпишете, изпратете „unsub“."
)
UNSUBSCRIBED = (
    "👋 Отписахте се и повече няма да получавате предупреждения.\n\n"
    "Ако размислите, изпратете „sub“, за да се абонирате отново."
)
NOT_SUBSCRIBED = (
    "ℹ️ Не сте абонирани, така че няма от какво да се отписвате.\n\n"
    "За да се абонирате, изпратете „sub“."
)
STATUS_SUBSCRIBED = "✅ Абонирани сте. За да се отпишете, изпратете „unsub“."
STATUS_NOT_SUBSCRIBED = "Не сте абонирани. За да се абонирате, изпратете „sub“."
HELP = (
    "Здравейте! Аз съм HailBot — изпращам предупреждения за градушка и "
    "опасно време в София.\n\n"
    f"{WHAT_YOU_GET}\n\n"
    "Команди:\n"
    "• „sub“ — абониране\n"
    "• „unsub“ — отписване\n"
    "• „status“ — проверка дали сте абонирани"
)


def normalize_command(text: object) -> str:
    """'/Sub@HailBot ' -> 'sub'."""
    words = str(text or "").strip().lower().split()
    if not words:
        return ""
    first = words[0].lstrip("/").split("@", 1)[0]
    return " ".join([first, *words[1:]])


class TelegramBot:
    """Turns incoming Telegram messages into subscription changes and replies."""

    def __init__(
        self,
        archive: ArchiveStore,
        notifier: TelegramNotifier,
        client: httpx.Client | None = None,
    ):
        self.archive = archive
        self.notifier = notifier
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=LONG_POLL_SECONDS + 15)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def reply_for(self, chat_id: str, text: object, now: datetime | None = None) -> str:
        """Apply the command in `text` for `chat_id` and return the reply."""
        command = normalize_command(text)
        if command in SUBSCRIBE_COMMANDS:
            added = self.archive.add_telegram_subscriber(
                chat_id,
                {"subscribed_at": (now or datetime.now(timezone.utc)).isoformat()},
            )
            if added:
                LOGGER.info("A Telegram chat subscribed")
            return WELCOME if added else ALREADY_SUBSCRIBED
        if command in UNSUBSCRIBE_COMMANDS:
            removed = self.archive.remove_telegram_subscriber(chat_id)
            if removed:
                LOGGER.info("A Telegram chat unsubscribed")
            return UNSUBSCRIBED if removed else NOT_SUBSCRIBED
        if command in STATUS_COMMANDS:
            subscribed = self.archive.is_telegram_subscriber(chat_id)
            return STATUS_SUBSCRIBED if subscribed else STATUS_NOT_SUBSCRIBED
        # /start, "help" and anything unrecognised get the introduction.
        return HELP

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or "id" not in chat or not isinstance(text, str):
            return
        chat_id = str(chat["id"])
        reply = self.reply_for(chat_id, text)
        try:
            self.notifier.send_message(reply, chat_id)
        except TelegramDeliveryError as error:
            LOGGER.warning("Telegram bot reply failed: %s", error)

    def fetch_updates(self, offset: int | None) -> list[dict[str, Any]]:
        # The URL carries the bot token, so never let it reach an error message.
        endpoint = f"{TELEGRAM_API_URL}/bot{self.notifier.settings.bot_token}/getUpdates"
        params: dict[str, Any] = {
            "timeout": LONG_POLL_SECONDS,
            "allowed_updates": '["message"]',
        }
        if offset is not None:
            params["offset"] = offset
        try:
            response = self.client.get(endpoint, params=params)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise TelegramDeliveryError("Telegram getUpdates request failed") from error
        if response.status_code != 200 or payload.get("ok") is not True:
            description = payload.get("description") if isinstance(payload, dict) else None
            detail = f": {' '.join(str(description).split())[:200]}" if description else ""
            raise TelegramDeliveryError(
                f"Telegram rejected getUpdates (HTTP {response.status_code}){detail}"
            )
        result = payload.get("result")
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def poll_once(self) -> int:
        """Handle one batch of updates; returns how many were handled."""
        state = self.archive.read_telegram_bot_state() or {}
        offset = state.get("next_update_offset")
        updates = self.fetch_updates(offset if isinstance(offset, int) else None)
        for update in updates:
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                continue
            try:
                self.handle_update(update)
            except Exception:  # one bad message must not wedge the bot
                LOGGER.exception("Telegram bot could not handle an update")
            # Confirm each update as soon as it is handled so a restart never
            # replays a command (and its reply) twice.
            self.archive.write_telegram_bot_state({"next_update_offset": update_id + 1})
        return len(updates)

    def run_forever(self) -> None:
        LOGGER.info("Telegram bot is listening for sub/unsub commands")
        while True:
            try:
                self.poll_once()
            except TelegramDeliveryError as error:
                LOGGER.warning("%s; retrying in %s s", error, ERROR_BACKOFF_SECONDS)
                time.sleep(ERROR_BACKOFF_SECONDS)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO, and those URLs contain the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_environment()
    if not settings.telegram.enabled or not settings.telegram.subscriptions_enabled:
        LOGGER.info("Telegram subscriptions are turned off; the bot is not running")
        return
    archive = ArchiveStore(settings.archive_root)
    notifier = TelegramNotifier(settings.telegram)
    bot = TelegramBot(archive, notifier)
    try:
        bot.run_forever()
    finally:
        bot.close()
        notifier.close()


if __name__ == "__main__":
    main()
