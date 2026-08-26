from __future__ import annotations

from datetime import timezone
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from .archive import ArchiveStore
from .config import Settings
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
    telegram_notifier = (
        TelegramNotifier(settings.telegram) if settings.telegram.enabled else None
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

    def poll_sources() -> None:
        if opera_service is not None:
            opera_service.poll_once()
        if volume_service is not None:
            volume_service.poll_once()

    scheduler = BlockingScheduler(timezone=timezone.utc)
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


if __name__ == "__main__":
    main()
