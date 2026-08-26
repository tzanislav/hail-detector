from __future__ import annotations

from datetime import timezone
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from .archive import ArchiveStore
from .config import Settings
from .opera import OperaClient, OperaIngestionService
from .opera_volume import OperaVolumeClient, OperaVolumeIngestionService
from .service import IngestionService
from .yarlovo import YarlovoClient


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_environment()
    archive = ArchiveStore(settings.archive_root)

    with YarlovoClient(settings.manifest_url, settings.source_timezone) as client:
        service = IngestionService(settings, archive, client)
        opera_client = (
            OperaClient(settings.opera_api_url, settings.opera_lookback)
            if settings.opera_enabled
            else None
        )
        opera_service = (
            OperaIngestionService(settings, archive, opera_client)
            if opera_client is not None
            else None
        )
        opera_volume_client = (
            OperaVolumeClient(settings.opera_volume_api_url, settings.opera_volume_lookback)
            if settings.opera_volume_enabled
            else None
        )
        opera_volume_service = (
            OperaVolumeIngestionService(settings, archive, opera_volume_client)
            if opera_volume_client is not None
            else None
        )

        def poll_opera_sources() -> None:
            # A volume profile is sampled against the latest primary tracks, so
            # keep these two OPERA polls ordered rather than running them in
            # independent scheduler jobs.
            if opera_service is not None:
                opera_service.poll_once()
            if opera_volume_service is not None:
                opera_volume_service.poll_once()

        scheduler = BlockingScheduler(timezone=timezone.utc)
        scheduler.add_job(
            service.poll_once,
            trigger="interval",
            seconds=settings.poll_interval_seconds,
            id="yarlovo-poll",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=settings.poll_interval_seconds,
        )
        if opera_service is not None or opera_volume_service is not None:
            scheduler.add_job(
                poll_opera_sources,
                trigger="interval",
                seconds=settings.poll_interval_seconds,
                id="opera-poll",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=settings.poll_interval_seconds,
            )
        try:
            poll_opera_sources()
            service.poll_once()
            scheduler.start()
        finally:
            if opera_client is not None:
                opera_client.close()
            if opera_volume_client is not None:
                opera_volume_client.close()


if __name__ == "__main__":
    main()
