from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any

import httpx

from .archive import ArchiveStore
from .config import TelegramSettings


LOGGER = logging.getLogger(__name__)
TELEGRAM_API_URL = "https://api.telegram.org"


class TelegramDeliveryError(RuntimeError):
    """A safe-to-log failure while talking to Telegram."""


class TelegramChatUnreachable(TelegramDeliveryError):
    """Telegram refused the chat for good (HTTP 403), e.g. the user blocked the bot."""


@dataclass(frozen=True)
class TelegramDeliveryResult:
    attempted: int = 0
    delivered: int = 0
    failed: int = 0
    skipped_stale: int = 0


class TelegramNotifier:
    """Send audited lifecycle transitions through Telegram's Bot API."""

    def __init__(
        self,
        settings: TelegramSettings,
        client: httpx.Client | None = None,
    ):
        if not settings.enabled:
            raise ValueError("TelegramNotifier requires enabled Telegram settings")
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=settings.request_timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    @staticmethod
    def _message_for(event: dict[str, Any]) -> str:
        level = str(event["level"])
        kind = str(event["kind"])
        previous_level = event.get("previous_level")
        if kind == "activated":
            headline = f"{level} activated"
        elif kind == "escalated":
            headline = f"{level} escalated from {previous_level}"
        elif kind == "deescalated":
            headline = f"{level} downgraded from {previous_level}"
        elif kind == "expired":
            headline = f"{level} expired"
        else:
            headline = f"{level} update ({kind})"

        return "\n".join(
            (
                "Radar-signature hail alert — not an official warning",
                headline,
                "Source: OPERA DBZH maximum-reflectivity composite",
                f"Track: {event['track_id']}",
                f"Time: {event['occurred_at']}",
                f"Reason: {event['reason']}",
            )
        )

    def _send_text(self, text: str, chat_id: str | None = None) -> None:
        # Do not include the request URL in errors/logs: it contains the bot
        # token. A short Telegram API description is safe and useful here.
        endpoint = f"{TELEGRAM_API_URL}/bot{self.settings.bot_token}/sendMessage"
        try:
            response = self.client.post(
                endpoint,
                json={"chat_id": chat_id or self.settings.chat_id, "text": text},
            )
        except httpx.HTTPError as error:
            raise TelegramDeliveryError("Telegram network request failed") from error
        if response.status_code < 200 or response.status_code >= 300:
            description = None
            try:
                value = response.json().get("description")
                if isinstance(value, str):
                    description = " ".join(value.split())[:200]
            except ValueError:
                pass
            detail = f": {description}" if description else ""
            error_type = (
                TelegramChatUnreachable if response.status_code == 403 else TelegramDeliveryError
            )
            raise error_type(
                f"Telegram rejected the request (HTTP {response.status_code}){detail}"
            )
        try:
            accepted = response.json().get("ok") is True
        except ValueError as error:
            raise TelegramDeliveryError("Telegram returned an invalid response") from error
        if not accepted:
            raise TelegramDeliveryError("Telegram did not accept the message")

    def send_message(self, text: str, chat_id: str | None = None) -> None:
        """Send a plain-text message to one chat (the owner chat by default).

        Raises TelegramDeliveryError on failure.
        """
        self._send_text(text, chat_id)

    def send_test_message(self) -> None:
        self._send_text(
            "Hail detector Telegram delivery is connected. "
            "This is a test message, not a weather alert."
        )

    def recipients(self, archive: ArchiveStore) -> list[str]:
        """The owner chat first, then every subscriber, without duplicates."""
        owner = str(self.settings.chat_id)
        chats = [owner]
        if self.settings.subscriptions_enabled:
            chats.extend(
                chat_id for chat_id in archive.read_telegram_subscriber_ids() if chat_id != owner
            )
        return chats

    def deliver_to(self, archive: ArchiveStore, chat_id: str, text: str) -> str:
        """Send an alert to one recipient.

        Returns "delivered", or "removed" when a subscriber has blocked the bot
        (they are unsubscribed and need no retry). Raises TelegramDeliveryError
        for failures worth retrying, including any failure for the owner chat.
        """
        try:
            self._send_text(text, chat_id)
        except TelegramChatUnreachable:
            if chat_id == str(self.settings.chat_id):
                raise
            archive.remove_telegram_subscriber(chat_id)
            LOGGER.info("Removed a Telegram subscriber that can no longer be reached")
            return "removed"
        return "delivered"

    def deliver_pending(
        self,
        archive: ArchiveStore,
        *,
        now: datetime | None = None,
    ) -> TelegramDeliveryResult:
        delivered_at = now or datetime.now(timezone.utc)
        if delivered_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        attempted = delivered = failed = skipped_stale = 0
        for event in archive.read_pending_notification_events():
            occurred_at = datetime.fromisoformat(str(event["occurred_at"]))
            if occurred_at.tzinfo is None:
                raise ValueError("notification outbox event must have a timezone-aware time")
            age = delivered_at - occurred_at.astimezone(timezone.utc)
            if age > self.settings.maximum_event_age:
                archive.write_notification_delivery(
                    str(event["event_id"]),
                    {
                        "event_id": event["event_id"],
                        "status": "skipped_stale",
                        "recorded_at": delivered_at.isoformat(),
                        "reason": "the alert transition was too old for a timely phone notification",
                    },
                )
                skipped_stale += 1
                continue

            attempted += 1
            event_id = str(event["event_id"])
            text = self._message_for(event)
            done = archive.read_notification_progress(event_id)
            errors: list[str] = []
            for chat_id in self.recipients(archive):
                if chat_id in done:
                    continue
                try:
                    self.deliver_to(archive, chat_id, text)
                except TelegramDeliveryError as error:
                    errors.append(str(error))
                    continue
                done.add(chat_id)

            if errors:
                # The event stays in the outbox without a terminal receipt, so
                # the next fresh poll retries only the chats that missed it.
                # The message never includes the bot-token-bearing request URL.
                archive.write_notification_progress(event_id, done)
                LOGGER.warning(
                    "Telegram delivery failed for event %s to %s chat(s): %s",
                    event_id,
                    len(errors),
                    errors[0],
                )
                failed += 1
                continue

            archive.write_notification_delivery(
                event_id,
                {
                    "event_id": event["event_id"],
                    "status": "delivered",
                    "delivered_at": delivered_at.isoformat(),
                    "recipient_count": len(done),
                },
            )
            delivered += 1

        return TelegramDeliveryResult(
            attempted=attempted,
            delivered=delivered,
            failed=failed,
            skipped_stale=skipped_stale,
        )
