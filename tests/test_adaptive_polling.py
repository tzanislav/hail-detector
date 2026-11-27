from datetime import timedelta
from pathlib import Path

from hail_detector.adaptive_polling import (
    RAPID_POLL_HOLD_UPDATES,
    RAPID_POLL_INTERVAL_SECONDS,
    AdaptivePollingCadence,
)
from hail_detector.archive import ArchiveStore
from hail_detector.config import Settings


def _settings(root: Path) -> Settings:
    return Settings(
        archive_root=root,
        poll_interval_seconds=30 * 60,
        tracker_maximum_gap=timedelta(minutes=12),
        tracker_maximum_speed_kmh=160,
    )


def _write_track(archive: ArchiveStore, *, latitude: float, longitude: float) -> None:
    archive.write_opera_tracker_state(
        {
            "tracks": [
                {
                    "track_id": "track-000001",
                    "latest_centroid": {"latitude": latitude, "longitude": longitude},
                }
            ]
        }
    )


def test_nearby_track_starts_five_following_rapid_automatic_updates(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    archive = ArchiveStore(tmp_path)
    cadence = AdaptivePollingCadence(settings, archive)
    _write_track(archive, latitude=42.6977, longitude=24.0)

    detection = cadence.record_automatic_primary_poll(primary_data_is_fresh=True)

    assert detection.interval_seconds == RAPID_POLL_INTERVAL_SECONDS
    assert detection.rapid_updates_remaining == RAPID_POLL_HOLD_UPDATES
    assert detection.nearby_track_detected is True

    # The track disappeared in the next source update. Its detection still
    # schedules five *following* automatic updates at the rapid cadence.
    archive.write_opera_tracker_state({"tracks": []})
    following_intervals = [
        cadence.record_automatic_primary_poll(primary_data_is_fresh=True).interval_seconds
        for _ in range(RAPID_POLL_HOLD_UPDATES)
    ]

    assert following_intervals == [
        RAPID_POLL_INTERVAL_SECONDS,
        RAPID_POLL_INTERVAL_SECONDS,
        RAPID_POLL_INTERVAL_SECONDS,
        RAPID_POLL_INTERVAL_SECONDS,
        settings.poll_interval_seconds,
    ]
    assert archive.read_opera_polling_state() == {
        "mode": "standard",
        "nearby_track_detected": False,
        "nearby_track_distance_km": 50.0,
        "next_poll_interval_seconds": settings.poll_interval_seconds,
        "rapid_poll_interval_seconds": RAPID_POLL_INTERVAL_SECONDS,
        "rapid_updates_remaining": 0,
        "standard_poll_interval_seconds": settings.poll_interval_seconds,
    }


def test_stale_primary_data_cannot_start_rapid_polling_from_an_old_track(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    archive = ArchiveStore(tmp_path)
    cadence = AdaptivePollingCadence(settings, archive)
    _write_track(archive, latitude=42.6977, longitude=24.0)

    decision = cadence.record_automatic_primary_poll(primary_data_is_fresh=False)

    assert decision.interval_seconds == settings.poll_interval_seconds
    assert decision.rapid_updates_remaining == 0
    assert decision.nearby_track_detected is False
