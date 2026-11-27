from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from hail_detector.archive import ArchiveStore
from hail_detector.config import SofiaWarningAlertSettings, TelegramSettings
from hail_detector.telegram import TelegramNotifier
from hail_detector.warning_alerts import SofiaWarningAlerter, qualifying_sofia_warnings
from hail_detector.warnings import WarningsIngestionService


# November: Sofia is UTC+2, so 10:00 UTC is 12:00 local time.
MIDDAY = datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc)
MIDNIGHT = datetime(2026, 11, 28, 22, 0, tzinfo=timezone.utc)  # 00:00 Sofia, Nov 29


def _warning(
    severity: str = "Severe",
    areas: list[str] | None = None,
    expires: str = "2026-11-30T22:00:00+00:00",
    event: str = "Значителни валежи и силен вятър!",
) -> dict[str, object]:
    return {
        "alert_id": "alert-1",
        "event": event,
        "severity": severity,
        "effective": "2026-11-28T06:00:00+00:00",
        "onset": None,
        "expires": expires,
        "headline": None,
        "description": "Очакват се интензивни валежи.",
        "instruction": None,
        "areas": areas if areas is not None else ["Перник", "София град"],
    }


class _Telegram:
    def __init__(self, fail: bool = False):
        self.messages: list[str] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})
        self.messages.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})


def _alerter(tmp_path, telegram: _Telegram, **settings) -> SofiaWarningAlerter:
    notifier = TelegramNotifier(
        TelegramSettings(enabled=True, bot_token="test-token", chat_id="12345"),
        httpx.Client(transport=httpx.MockTransport(telegram.handler)),
    )
    return SofiaWarningAlerter(
        SofiaWarningAlertSettings(**settings), ArchiveStore(tmp_path), notifier
    )


def test_only_severe_or_extreme_unexpired_sofia_city_warnings_qualify() -> None:
    warnings = [
        _warning("Severe"),
        _warning("Extreme"),
        _warning("Moderate"),
        _warning("Extreme", areas=["Софийска област", "Перник"]),
        _warning("Severe", expires="2026-11-28T09:59:00+00:00"),
        _warning("Severe", areas=["Sofia-city"]),
    ]
    qualifying = qualifying_sofia_warnings(warnings, MIDDAY)
    assert [w["severity"] for w in qualifying] == ["Severe", "Extreme", "Severe"]


def test_sends_once_per_day_and_again_the_next_day(tmp_path) -> None:
    telegram = _Telegram()
    alerter = _alerter(tmp_path, telegram)
    warnings = [_warning("Extreme")]

    assert alerter.process(warnings, MIDDAY).status == "sent"
    assert alerter.process(warnings, MIDDAY + timedelta(hours=2)).status == "already_sent_today"
    assert len(telegram.messages) == 1
    message = telegram.messages[0]
    assert "Official weather warning — Sofia city" in message
    assert "🔴 Red / Extreme" in message
    assert "Sat 28 Nov 08:00 → Tue 01 Dec 00:00 (Sofia time)" in message

    next_day = MIDDAY + timedelta(days=1)
    assert alerter.process(warnings, next_day).status == "sent"
    assert len(telegram.messages) == 2
    assert "Updated" not in telegram.messages[1]


def test_new_or_changed_warning_sends_an_update_the_same_day(tmp_path) -> None:
    telegram = _Telegram()
    alerter = _alerter(tmp_path, telegram)
    assert alerter.process([_warning("Severe")], MIDDAY).status == "sent"

    escalated = [_warning("Extreme")]
    assert alerter.process(escalated, MIDDAY + timedelta(hours=1)).status == "sent_update"
    assert "Updated official weather warning" in telegram.messages[1]
    assert "(new/updated)" in telegram.messages[1]

    # A warning that only disappears is not an update worth a message.
    assert alerter.process(escalated, MIDDAY + timedelta(hours=2)).status == "already_sent_today"
    assert len(telegram.messages) == 2


def test_quiet_hours_defer_until_morning_unless_the_warning_has_ended(tmp_path) -> None:
    telegram = _Telegram()
    alerter = _alerter(tmp_path, telegram)
    overnight = [_warning("Severe")]
    short_lived = [_warning("Extreme", expires="2026-11-29T05:00:00+00:00")]  # 07:00 Sofia

    assert alerter.process(overnight, MIDNIGHT).status == "deferred_quiet_hours"
    assert alerter.process(short_lived, MIDNIGHT).status == "deferred_quiet_hours"
    morning = datetime(2026, 11, 29, 7, 0, tzinfo=timezone.utc)  # 09:00 Sofia
    assert alerter.process(short_lived, morning).status == "no_qualifying_warning"
    assert alerter.process(overnight, morning).status == "sent"
    assert len(telegram.messages) == 1


@pytest.mark.parametrize(
    ("utc_hour", "quiet"),
    [(20, False), (21, True), (22, True), (6, True), (7, False)],  # 22:00, 23:00, 00:00, 08:00, 09:00 Sofia
)
def test_quiet_hours_are_23_to_09_sofia_time(tmp_path, utc_hour: int, quiet: bool) -> None:
    alerter = _alerter(tmp_path, _Telegram())
    assert alerter.is_quiet_time(datetime(2026, 11, 28, utc_hour, tzinfo=timezone.utc)) is quiet


def test_failed_delivery_is_retried_on_the_next_poll(tmp_path) -> None:
    telegram = _Telegram(fail=True)
    alerter = _alerter(tmp_path, telegram)
    result = alerter.process([_warning()], MIDDAY)
    assert result.status == "delivery_failed"
    assert "test-token" not in (result.error or "")

    telegram.fail = False
    assert alerter.process([_warning()], MIDDAY + timedelta(minutes=30)).status == "sent"


def test_ingestion_poll_triggers_the_sofia_alerter(tmp_path) -> None:
    class StubClient:
        def fetch_bulgaria_warnings(self, now):
            return (_warning("Severe"),)

    telegram = _Telegram()
    alerter = _alerter(tmp_path, telegram)
    service = WarningsIngestionService(ArchiveStore(tmp_path), StubClient(), alerter)
    assert service.poll_once(MIDDAY).state == "ready"
    assert len(telegram.messages) == 1


def test_invalid_quiet_hours_are_rejected() -> None:
    with pytest.raises(ValueError):
        SofiaWarningAlertSettings(quiet_start_hour=9, quiet_end_hour=9)
    with pytest.raises(ValueError):
        SofiaWarningAlertSettings(timezone="Not/AZone")
