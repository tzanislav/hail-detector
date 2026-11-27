from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx

from hail_detector.archive import ArchiveStore
from hail_detector.config import SofiaWarningAlertSettings, TelegramSettings
from hail_detector.telegram import TelegramNotifier
from hail_detector import telegram_bot
from hail_detector.telegram_bot import TelegramBot, normalize_command
from hail_detector.warning_alerts import SofiaWarningAlerter


NOW = datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc)  # 12:00 in Sofia
OWNER = "12345"


class _Telegram:
    """Fake Bot API: records sendMessage calls; some chats can fail."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.blocked: set[str] = set()
        self.failing: set[str] = set()
        self.updates: list[dict[str, object]] = []
        self.get_updates_params: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            self.get_updates_params.append(dict(request.url.params))
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        payload = json.loads(request.content)
        chat_id = str(payload["chat_id"])
        if chat_id in self.blocked:
            return httpx.Response(
                403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}
            )
        if chat_id in self.failing:
            return httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})
        self.sent.append((chat_id, payload["text"]))
        return httpx.Response(200, json={"ok": True})

    def chats(self) -> list[str]:
        return [chat_id for chat_id, _ in self.sent]


def _notifier(telegram: _Telegram, **settings) -> TelegramNotifier:
    return TelegramNotifier(
        TelegramSettings(enabled=True, bot_token="test-token", chat_id=OWNER, **settings),
        httpx.Client(transport=httpx.MockTransport(telegram.handler)),
    )


def _bot(tmp_path: Path, telegram: _Telegram) -> TelegramBot:
    client = httpx.Client(transport=httpx.MockTransport(telegram.handler))
    return TelegramBot(ArchiveStore(tmp_path), _notifier(telegram), client)


def _event(event_id: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "kind": "activated",
        "track_id": "track-000001",
        "level": "Severe Reflectivity Alert",
        "previous_level": None,
        "occurred_at": NOW.isoformat(),
        "reason": "test",
    }


def _warning() -> dict[str, object]:
    return {
        "event": "Силен вятър",
        "severity": "Severe",
        "effective": "2026-11-28T06:00:00+00:00",
        "expires": "2026-11-30T22:00:00+00:00",
        "description": "Очаква се силен вятър.",
        "areas": ["София град"],
    }


def test_commands_are_normalized() -> None:
    assert normalize_command("/Sub@HailBot") == "sub"
    assert normalize_command("  SUB ") == "sub"
    assert normalize_command("Абонирай ме") == "абонирай ме"
    assert normalize_command(None) == ""


def test_sub_and_unsub_replies_cover_every_state(tmp_path: Path) -> None:
    bot = _bot(tmp_path, _Telegram())

    assert bot.reply_for("777", "unsub") == telegram_bot.NOT_SUBSCRIBED
    assert bot.reply_for("777", "/status") == telegram_bot.STATUS_NOT_SUBSCRIBED
    welcome = bot.reply_for("777", "sub")
    assert welcome == telegram_bot.WELCOME
    assert "unsub" in welcome  # tells people how to leave
    assert bot.reply_for("777", "/subscribe") == telegram_bot.ALREADY_SUBSCRIBED
    assert bot.reply_for("777", "status") == telegram_bot.STATUS_SUBSCRIBED
    assert bot.archive.read_telegram_subscriber_ids() == ["777"]
    assert bot.reply_for("777", "/stop") == telegram_bot.UNSUBSCRIBED
    assert bot.reply_for("777", "unsub") == telegram_bot.NOT_SUBSCRIBED
    assert bot.archive.read_telegram_subscriber_ids() == []
    assert bot.reply_for("777", "/start") == telegram_bot.HELP
    assert bot.reply_for("777", "здрасти") == telegram_bot.HELP


def test_poll_once_replies_and_never_replays_an_update(tmp_path: Path) -> None:
    telegram = _Telegram()
    telegram.updates = [
        {"update_id": 41, "message": {"chat": {"id": 777}, "text": "sub"}},
        {"update_id": 42, "message": {"chat": {"id": 888}, "text": "/start"}},
        {"update_id": 43, "edited_message": {"chat": {"id": 888}, "text": "sub"}},
    ]
    bot = _bot(tmp_path, telegram)

    assert bot.poll_once() == 3
    assert telegram.sent == [("777", telegram_bot.WELCOME), ("888", telegram_bot.HELP)]
    assert bot.archive.read_telegram_subscriber_ids() == ["777"]
    assert bot.archive.read_telegram_bot_state() == {"next_update_offset": 44}

    telegram.updates = []
    bot.poll_once()
    assert telegram.get_updates_params[-1]["offset"] == "44"


def test_radar_alert_reaches_owner_and_subscribers_and_retries_only_misses(
    tmp_path: Path,
) -> None:
    archive = ArchiveStore(tmp_path)
    for chat_id in ("777", "888", "999", OWNER):
        archive.add_telegram_subscriber(chat_id, {})
    archive.write_notification_outbox(_event("event-1"))
    telegram = _Telegram()
    telegram.failing.add("888")
    telegram.blocked.add("999")
    notifier = _notifier(telegram)

    first = notifier.deliver_pending(archive, now=NOW)
    assert first.failed == 1
    assert sorted(telegram.chats()) == sorted([OWNER, "777"])  # owner not sent twice
    assert archive.read_telegram_subscriber_ids() == ["12345", "777", "888"]  # 999 removed

    telegram.failing.clear()
    second = notifier.deliver_pending(archive, now=NOW + timedelta(minutes=1))
    assert second.delivered == 1
    assert sorted(telegram.chats()) == sorted([OWNER, "777", "888"])
    assert notifier.deliver_pending(archive, now=NOW + timedelta(minutes=2)).attempted == 0


def test_subscriptions_can_be_turned_off(tmp_path: Path) -> None:
    archive = ArchiveStore(tmp_path)
    archive.add_telegram_subscriber("777", {})
    archive.write_notification_outbox(_event("event-1"))
    telegram = _Telegram()

    _notifier(telegram, subscriptions_enabled=False).deliver_pending(archive, now=NOW)
    assert telegram.chats() == [OWNER]


def test_sofia_warning_goes_to_each_chat_once_and_to_late_subscribers(tmp_path: Path) -> None:
    archive = ArchiveStore(tmp_path)
    archive.add_telegram_subscriber("777", {})
    telegram = _Telegram()
    alerter = SofiaWarningAlerter(SofiaWarningAlertSettings(), archive, _notifier(telegram))

    assert alerter.process([_warning()], NOW).status == "sent"
    assert sorted(telegram.chats()) == sorted([OWNER, "777"])
    assert alerter.process([_warning()], NOW + timedelta(hours=1)).status == "already_sent_today"

    archive.add_telegram_subscriber("888", {})
    assert alerter.process([_warning()], NOW + timedelta(hours=2)).status == "sent"
    assert telegram.chats()[-1] == "888"
    assert len(telegram.sent) == 3


def test_sofia_warning_state_from_before_subscriptions_still_counts_for_owner(
    tmp_path: Path,
) -> None:
    archive = ArchiveStore(tmp_path)
    telegram = _Telegram()
    alerter = SofiaWarningAlerter(SofiaWarningAlertSettings(), archive, _notifier(telegram))
    alerter.process([_warning()], NOW)
    legacy = archive.read_sofia_warning_alert_state()["recipients"][OWNER]
    archive.write_sofia_warning_alert_state(legacy)  # the old, single-chat shape

    assert alerter.process([_warning()], NOW + timedelta(hours=1)).status == "already_sent_today"
    assert len(telegram.sent) == 1
