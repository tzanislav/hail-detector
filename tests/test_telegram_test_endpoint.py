from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

import hail_detector.api as api_module
from hail_detector.api import create_app
from hail_detector.config import Settings, TelegramSettings
from hail_detector.telegram import TelegramDeliveryError


class FakeNotifier:
    sent: list[str] = []
    fail = False

    def __init__(self, settings: TelegramSettings):
        self.settings = settings

    def send_message(self, text: str) -> None:
        if FakeNotifier.fail:
            raise TelegramDeliveryError("Telegram rejected the request (HTTP 400): chat not found")
        FakeNotifier.sent.append(text)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_notifier(monkeypatch):
    FakeNotifier.sent = []
    FakeNotifier.fail = False
    monkeypatch.setattr(api_module, "TelegramNotifier", FakeNotifier)
    # Disable the guessing throttle so each test can make several attempts.
    monkeypatch.setattr(api_module, "TELEGRAM_TEST_MINIMUM_INTERVAL_SECONDS", 0.0)


def _app(root: Path, *, enabled: bool = True, password: str | None = "grilleD123"):
    settings = replace(
        Settings(
            archive_root=root,
            poll_interval_seconds=240,
            tracker_maximum_gap=timedelta(minutes=12),
            tracker_maximum_speed_kmh=160,
        ),
        telegram=TelegramSettings(
            enabled=enabled,
            bot_token="test-token" if enabled else None,
            chat_id="123" if enabled else None,
            test_password=password,
        ),
    )
    return create_app(settings)


async def _post(app, password: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/telegram/test", json={"password": password})


def test_correct_password_sends_a_test_message(tmp_path: Path) -> None:
    response = asyncio.run(_post(_app(tmp_path), "grilleD123"))
    assert response.status_code == 200
    assert response.json()["sent"] is True
    assert len(FakeNotifier.sent) == 1
    assert "test" in FakeNotifier.sent[0].lower()


def test_wrong_password_is_rejected_without_sending(tmp_path: Path) -> None:
    app = _app(tmp_path)
    for attempt in ("", "grilled123", "grilleD1234"):
        response = asyncio.run(_post(app, attempt))
        assert response.status_code == 401
        assert response.json()["detail"] == "Incorrect password."
    assert FakeNotifier.sent == []


def test_rapid_attempts_are_throttled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(api_module, "TELEGRAM_TEST_MINIMUM_INTERVAL_SECONDS", 60.0)
    app = _app(tmp_path)
    assert asyncio.run(_post(app, "wrong")).status_code == 401
    assert asyncio.run(_post(app, "grilleD123")).status_code == 429
    assert FakeNotifier.sent == []


def test_disabled_telegram_or_missing_password_cannot_be_tested(tmp_path: Path) -> None:
    disabled = asyncio.run(_post(_app(tmp_path, enabled=False), "grilleD123"))
    unconfigured = asyncio.run(_post(_app(tmp_path, password=None), "grilleD123"))
    assert disabled.status_code == 409
    assert unconfigured.status_code == 409
    assert "HAIL_TELEGRAM_TEST_PASSWORD" in unconfigured.json()["detail"]
    assert FakeNotifier.sent == []


def test_telegram_failure_is_reported(tmp_path: Path) -> None:
    FakeNotifier.fail = True
    response = asyncio.run(_post(_app(tmp_path), "grilleD123"))
    assert response.status_code == 502
    assert "chat not found" in response.json()["detail"]
    assert "test-token" not in response.text


def test_password_is_not_exposed_in_settings_repr() -> None:
    settings = TelegramSettings(enabled=True, bot_token="t", chat_id="1", test_password="grilleD123")
    assert "grilleD123" not in repr(settings)
