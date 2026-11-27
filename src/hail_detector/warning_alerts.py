"""Telegram messages for official Severe/Extreme warnings that cover Sofia city.

These rules apply only to official MeteoAlarm warnings. Radar hail alerts are
sent through the separate OPERA notification outbox and are never delayed or
suppressed by anything in this module.

Delivery rules:
- only Severe (orange) or Extreme (red) warnings whose areas include Sofia city;
- a warning whose end time has already passed is never sent;
- nothing is sent during the configured quiet hours (default 23:00-09:00
  Sofia time); a warning that arrived overnight is sent once quiet hours end,
  provided it has not expired by then;
- at most one message per local day, unless a new or changed warning appears,
  in which case an update message is sent.

These rules are tracked per recipient (the owner chat and every bot
subscriber), so someone who subscribes mid-day still gets the day's warning on
the next poll, and a failed send is retried only for the chat that missed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import threading
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .archive import ArchiveStore
from .config import SofiaWarningAlertSettings
from .telegram import TelegramDeliveryError, TelegramNotifier


LOGGER = logging.getLogger(__name__)

# MeteoAlarm area names (normally Bulgarian) that denote Sofia city. Sofia
# province ("Софийска област") is a separate area and is deliberately excluded.
# Keep in step with SOFIA_AREA_NAMES in static/dashboard.js.
SOFIA_AREA_NAMES = frozenset(
    {"софия град", "град софия", "софия", "sofia city", "sofia grad", "sofia"}
)
NOTIFIABLE_SEVERITIES = {"severe": ("🟠", "Orange"), "extreme": ("🔴", "Red")}
MAX_DESCRIPTION_CHARACTERS = 600
TELEGRAM_TEXT_LIMIT = 4000


def _normalize_area(area: object) -> str:
    text = str(area or "").lower()
    for separator in "-–_":
        text = text.replace(separator, " ")
    return " ".join(text.split())


def is_sofia_area(area: object) -> bool:
    return _normalize_area(area) in SOFIA_AREA_NAMES


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def qualifying_sofia_warnings(
    warnings: Iterable[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Severe/Extreme warnings for Sofia city whose end time has not passed."""
    qualifying = []
    for warning in warnings:
        if str(warning.get("severity") or "").lower() not in NOTIFIABLE_SEVERITIES:
            continue
        areas = warning.get("areas")
        if not isinstance(areas, list) or not any(is_sofia_area(area) for area in areas):
            continue
        expires = _parse_time(warning.get("expires"))
        if expires is not None and expires <= now:
            continue
        qualifying.append(warning)
    return qualifying


def warning_fingerprint(warning: dict[str, Any]) -> str:
    """Content identity: a change to any of these fields counts as an update."""
    content = {
        key: warning.get(key)
        for key in (
            "event",
            "severity",
            "effective",
            "onset",
            "expires",
            "headline",
            "description",
            "instruction",
        )
    }
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


@dataclass(frozen=True)
class SofiaWarningAlertResult:
    status: str
    warning_count: int = 0
    error: str | None = None


class SofiaWarningAlerter:
    """Decide whether to send a Sofia warning message and send it."""

    def __init__(
        self,
        settings: SofiaWarningAlertSettings,
        archive: ArchiveStore,
        notifier: TelegramNotifier,
    ):
        self.settings = settings
        self.archive = archive
        self.notifier = notifier
        self.timezone = ZoneInfo(settings.timezone)
        # The poller can run a scheduled poll and the quiet-hours-end poll at
        # the same moment; never let both send.
        self._lock = threading.Lock()

    def is_quiet_time(self, now: datetime) -> bool:
        hour = now.astimezone(self.timezone).hour
        start, end = self.settings.quiet_start_hour, self.settings.quiet_end_hour
        if start > end:  # wraps past midnight, e.g. 23:00-09:00
            return hour >= start or hour < end
        return start <= hour < end

    def process(
        self, warnings: Iterable[dict[str, Any]], now: datetime
    ) -> SofiaWarningAlertResult:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        with self._lock:
            qualifying = qualifying_sofia_warnings(warnings, now)
            if not qualifying:
                return SofiaWarningAlertResult("no_qualifying_warning")
            if self.is_quiet_time(now):
                return SofiaWarningAlertResult("deferred_quiet_hours", len(qualifying))

            local_date = now.astimezone(self.timezone).date().isoformat()
            fingerprints = [warning_fingerprint(warning) for warning in qualifying]
            recipients = self.notifier.recipients(self.archive)
            state = self._recipient_state()
            # Forget chats that have unsubscribed so the state cannot grow forever.
            state = {chat_id: entry for chat_id, entry in state.items() if chat_id in recipients}

            sent_kinds: list[str] = []
            errors: list[str] = []
            for chat_id in recipients:
                entry = state.get(chat_id) or {}
                sent_today = entry.get("last_sent_local_date") == local_date
                previously_sent = (
                    set(entry.get("sent_fingerprints") or []) if sent_today else set()
                )
                changed = {fp for fp in fingerprints if fp not in previously_sent}
                if sent_today and not changed:
                    continue
                text = self._message(qualifying, fingerprints, changed if sent_today else set())
                try:
                    outcome = self.notifier.deliver_to(self.archive, chat_id, text)
                except TelegramDeliveryError as error:
                    # No state is written for this chat, so the next poll retries it.
                    errors.append(str(error))
                    continue
                if outcome == "removed":
                    state.pop(chat_id, None)
                    continue
                state[chat_id] = {
                    "last_sent_local_date": local_date,
                    "last_sent_at": now.astimezone(timezone.utc).isoformat(),
                    "sent_fingerprints": sorted(previously_sent | set(fingerprints)),
                }
                sent_kinds.append("update" if sent_today else "new")

            self.archive.write_sofia_warning_alert_state({"recipients": state})
            if errors:
                LOGGER.warning(
                    "Sofia warning Telegram delivery failed for %s chat(s): %s",
                    len(errors),
                    errors[0],
                )
            if not sent_kinds:
                if errors:
                    return SofiaWarningAlertResult(
                        "delivery_failed", len(qualifying), errors[0]
                    )
                return SofiaWarningAlertResult("already_sent_today", len(qualifying))

            status = "sent_update" if all(kind == "update" for kind in sent_kinds) else "sent"
            LOGGER.info(
                "Sofia warning Telegram message %s to %s chat(s) (%s warnings)",
                status,
                len(sent_kinds),
                len(qualifying),
            )
            return SofiaWarningAlertResult(status, len(qualifying))

    def _recipient_state(self) -> dict[str, dict[str, Any]]:
        stored = self.archive.read_sofia_warning_alert_state() or {}
        recipients = stored.get("recipients")
        if isinstance(recipients, dict):
            return {str(chat_id): dict(entry) for chat_id, entry in recipients.items()}
        if "last_sent_local_date" in stored:
            # State written before subscriptions existed described the owner chat.
            return {
                str(self.notifier.settings.chat_id): {
                    key: stored.get(key)
                    for key in ("last_sent_local_date", "last_sent_at", "sent_fingerprints")
                }
            }
        return {}

    def _time_label(self, value: object) -> str:
        parsed = _parse_time(value)
        if parsed is None:
            return "unknown"
        return parsed.astimezone(self.timezone).strftime("%a %d %b %H:%M")

    def _message(
        self,
        warnings: list[dict[str, Any]],
        fingerprints: list[str],
        changed: set[str],
    ) -> str:
        title = (
            "Updated official weather warning — Sofia city (София град)"
            if changed
            else "Official weather warning — Sofia city (София град)"
        )
        blocks = [f"⚠️ {title}"]
        for warning, fingerprint in zip(warnings, fingerprints):
            severity = str(warning.get("severity") or "").lower()
            icon, colour = NOTIFIABLE_SEVERITIES[severity]
            event = warning.get("event") or "Official weather warning"
            marker = " (new/updated)" if fingerprint in changed else ""
            lines = [f"{icon} {colour} / {warning.get('severity')}: {event}{marker}"]
            headline = warning.get("headline")
            if headline and headline != event:
                lines.append(str(headline))
            start = warning.get("effective") or warning.get("onset")
            lines.append(
                f"Valid: {self._time_label(start)} → {self._time_label(warning.get('expires'))} (Sofia time)"
                if start
                else f"Valid until {self._time_label(warning.get('expires'))} (Sofia time)"
            )
            description = " ".join(str(warning.get("description") or "").split())
            if description:
                if len(description) > MAX_DESCRIPTION_CHARACTERS:
                    description = description[: MAX_DESCRIPTION_CHARACTERS - 1].rstrip() + "…"
                lines.append(description)
            blocks.append("\n".join(lines))
        blocks.append(
            "Source: MeteoAlarm (NIMH Bulgaria). Separate from the radar hail alerts."
        )
        text = "\n\n".join(blocks)
        if len(text) > TELEGRAM_TEXT_LIMIT:
            text = text[: TELEGRAM_TEXT_LIMIT - 1] + "…"
        return text
