from __future__ import annotations

from pathlib import Path

from hail_detector.config import Settings


def test_settings_load_opera_alert_and_telegram_configuration_from_dotenv(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "HAIL_SEVERE_MINIMUM_DBZ",
        "HAIL_SEVERE_MAXIMUM_LEAD_MINUTES",
        "HAIL_MINIMUM_TRAJECTORY_CONFIDENCE",
        "HAIL_TELEGRAM_ENABLED",
        "HAIL_TELEGRAM_BOT_TOKEN",
        "HAIL_TELEGRAM_CHAT_ID",
        "HAIL_LLM_ENABLED",
        "OPENAI_API_KEY",
        "HAIL_LLM_ALERT_THRESHOLD",
        "HAIL_LLM_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "HAIL_SEVERE_MINIMUM_DBZ=65\n"
        "HAIL_SEVERE_MAXIMUM_LEAD_MINUTES=30\n"
        "HAIL_MINIMUM_TRAJECTORY_CONFIDENCE=high\n"
        "HAIL_TELEGRAM_ENABLED=true\n"
        "HAIL_TELEGRAM_BOT_TOKEN=test-token\n"
        "HAIL_TELEGRAM_CHAT_ID=1234567890\n"
        "HAIL_LLM_ENABLED=true\n"
        "OPENAI_API_KEY=test-openai-key\n"
        "HAIL_LLM_ALERT_THRESHOLD=8\n"
        "HAIL_LLM_REASONING_EFFORT=high\n",
        encoding="utf-8",
    )

    settings = Settings.from_environment()

    assert settings.severe_alert.minimum_dbz == 65
    assert settings.severe_alert.maximum_lead_minutes == 30
    assert settings.severe_alert.minimum_trajectory_confidence == "high"
    assert settings.telegram.enabled is True
    assert settings.hail_risk_agent.enabled is True
    assert settings.hail_risk_agent.alert_threshold == 8
    assert settings.hail_risk_agent.reasoning_effort == "high"
