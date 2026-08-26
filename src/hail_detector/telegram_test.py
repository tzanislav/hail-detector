from __future__ import annotations

from .config import Settings
from .telegram import TelegramDeliveryError, TelegramNotifier


def main() -> None:
    try:
        settings = Settings.from_environment()
    except ValueError as error:
        raise SystemExit(f"Telegram configuration is invalid: {error}") from error
    if not settings.telegram.enabled:
        raise SystemExit(
            "Set HAIL_TELEGRAM_ENABLED=true in .env before sending a test message."
        )
    bot_id = str(settings.telegram.bot_token).partition(":")[0]
    if settings.telegram.chat_id == bot_id:
        raise SystemExit(
            "HAIL_TELEGRAM_CHAT_ID is the bot's own ID. Send the bot a message, "
            "then run: python -m hail_detector.telegram_chat_id"
        )

    notifier = TelegramNotifier(settings.telegram)
    try:
        notifier.send_test_message()
    except TelegramDeliveryError as error:
        raise SystemExit(f"Telegram test message was not delivered: {error}") from error
    finally:
        notifier.close()
    print("Telegram test message delivered.")


if __name__ == "__main__":
    main()
