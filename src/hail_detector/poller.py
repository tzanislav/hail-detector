from __future__ import annotations

from datetime import timezone
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from .adaptive_polling import AdaptivePollingCadence
from .archive import ArchiveStore
from .config import Settings
from .hail_risk_agent import HailRiskAgent
from .opera import OperaClient, OperaIngestionService
from .opera_volume import OperaVolumeClient, OperaVolumeIngestionService
from .telegram import TelegramNotifier


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_environment()
    archive = ArchiveStore(settings.archive_root)
    cadence = AdaptivePollingCadence(settings, archive)
    telegram_notifier = (
        TelegramNotifier(settings.telegram) if settings.telegram.enabled else None
    )
    hail_risk_agent = (
        HailRiskAgent(settings.hail_risk_agent)
        if settings.hail_risk_agent.enabled
        else None
    )
    opera_client = (
        OperaClient(settings.opera_api_url, settings.opera_lookback)
        if settings.opera_enabled
        else None
    )
    opera_service = (
        OperaIngestionService(
            settings,
            archive,
            opera_client,
            telegram_notifier=telegram_notifier,
            hail_risk_agent=hail_risk_agent,
        )
        if opera_client is not None
        else None
    )
    volume_client = (
        OperaVolumeClient(settings.opera_volume_api_url, settings.opera_volume_lookback)
        if settings.opera_volume_enabled
        else None
    )
    volume_service = (
        OperaVolumeIngestionService(settings, archive, volume_client)
        if volume_client is not None
        else None
    )

    scheduler = BlockingScheduler(timezone=timezone.utc)
    scheduled_interval_seconds = settings.poll_interval_seconds

    def poll_sources() -> None:
        nonlocal scheduled_interval_seconds
        if opera_service is not None:
            primary_result = opera_service.poll_once()
            next_cadence = cadence.record_automatic_primary_poll(
                primary_data_is_fresh=(
                    primary_result.state == "ready" and primary_result.analyzed_frames > 0
                )
            )
            if next_cadence.interval_seconds != scheduled_interval_seconds:
                scheduler.reschedule_job(
                    "opera-poll",
                    trigger="interval",
                    seconds=next_cadence.interval_seconds,
                )
                scheduled_interval_seconds = next_cadence.interval_seconds
            logging.getLogger(__name__).info(
                "OPERA polling cadence: %s; next update in %s minutes; "
                "%s rapid updates retained",
                next_cadence.mode,
                next_cadence.interval_seconds // 60,
                next_cadence.rapid_updates_remaining,
            )
        if volume_service is not None:
            volume_service.poll_once()

    if opera_service is not None or volume_service is not None:
        scheduler.add_job(
            poll_sources,
            trigger="interval",
            seconds=settings.poll_interval_seconds,
            id="opera-poll",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=settings.poll_interval_seconds,
        )
    try:
        poll_sources()
        scheduler.start()
    finally:
        if opera_client is not None:
            opera_client.close()
        if volume_client is not None:
            volume_client.close()
        if telegram_notifier is not None:
            telegram_notifier.close()
        if hail_risk_agent is not None:
            hail_risk_agent.close()


if __name__ == "__main__":
    main()
