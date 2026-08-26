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

    def _send_text(self, text: str) -> None:
        # Do not include the request URL in errors/logs: it contains the bot
        # token. A short Telegram API description is safe and useful here.
        endpoint = f"{TELEGRAM_API_URL}/bot{self.settings.bot_token}/sendMessage"
        try:
            response = self.client.post(
                endpoint,
                json={"chat_id": self.settings.chat_id, "text": text},
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
            raise TelegramDeliveryError(
                f"Telegram rejected the request (HTTP {response.status_code}){detail}"
            )
        try:
            accepted = response.json().get("ok") is True
        except ValueError as error:
            raise TelegramDeliveryError("Telegram returned an invalid response") from error
        if not accepted:
            raise TelegramDeliveryError("Telegram did not accept the message")

    def send_test_message(self) -> None:
        self._send_text(
            "Hail detector Telegram delivery is connected. "
            "This is a test message, not a weather alert."
        )

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
            try:
                self._send_text(self._message_for(event))
            except TelegramDeliveryError as error:
                # The event stays in the outbox without a terminal receipt, so
                # the next fresh poll can retry it. The message never includes
                # the bot-token-bearing request URL.
                LOGGER.warning("Telegram delivery failed for event %s: %s", event["event_id"], error)
                failed += 1
                continue

            archive.write_notification_delivery(
                str(event["event_id"]),
                {
                    "event_id": event["event_id"],
                    "status": "delivered",
                    "delivered_at": delivered_at.isoformat(),
                },
            )
            delivered += 1

        return TelegramDeliveryResult(
            attempted=attempted,
            delivered=delivered,
            failed=failed,
            skipped_stale=skipped_stale,
        )
