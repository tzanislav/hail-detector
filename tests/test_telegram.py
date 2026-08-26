from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx

from hail_detector.archive import ArchiveStore
from hail_detector.config import TelegramSettings
from hail_detector.telegram import TelegramNotifier


NOW = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)


def _event(event_id: str, occurred_at: datetime = NOW) -> dict[str, object]:
    return {
        "event_id": event_id,
        "kind": "activated",
        "track_id": "track-000001",
        "level": "Severe Reflectivity Alert",
        "source": "eumetnet_opera_dbzh_maximum_reflectivity",
        "previous_level": None,
        "occurred_at": occurred_at.isoformat(),
        "reason": "deterministic test candidate",
    }


def _notifier(handler) -> TelegramNotifier:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramNotifier(
        TelegramSettings(enabled=True, bot_token="test-token", chat_id="12345"),
        client,
    )


def test_telegram_delivers_an_outbox_event_once(tmp_path: Path) -> None:
    archive = ArchiveStore(tmp_path)
    archive.write_notification_outbox(_event("event-1"))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    notifier = _notifier(handler)
    first = notifier.deliver_pending(archive, now=NOW + timedelta(minutes=1))
    second = notifier.deliver_pending(archive, now=NOW + timedelta(minutes=2))

    assert first.delivered == 1
    assert first.attempted == 1
    assert second.attempted == 0
    assert len(requests) == 1
    assert requests[0].url.path == "/bottest-token/sendMessage"
    payload = json.loads(requests[0].content)
    assert payload["chat_id"] == "12345"
    assert "Radar-signature hail alert" in payload["text"]
    receipt = archive.notification_delivery_dir / "event-1.json"
    assert receipt.is_file()
    assert '"status": "delivered"' in receipt.read_text(encoding="utf-8")


def test_telegram_retries_failed_events_and_skips_old_events(tmp_path: Path) -> None:
    archive = ArchiveStore(tmp_path)
    archive.write_notification_outbox(_event("retry"))
    archive.write_notification_outbox(_event("old", NOW - timedelta(minutes=11)))
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500 if calls == 1 else 200, json={"ok": calls != 1})

    notifier = _notifier(handler)
    failed = notifier.deliver_pending(archive, now=NOW)
    retried = notifier.deliver_pending(archive, now=NOW + timedelta(minutes=1))

    assert failed.failed == 1
    assert failed.skipped_stale == 1
    assert retried.delivered == 1
    assert calls == 2
    assert (archive.notification_delivery_dir / "old.json").is_file()
    assert (archive.notification_delivery_dir / "retry.json").is_file()
