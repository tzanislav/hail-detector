from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from hail_detector.api import create_app
from hail_detector.archive import ArchiveStore
from hail_detector.config import Settings
from hail_detector.models import RadarFrame


def _settings(root: Path) -> Settings:
    return Settings(
        archive_root=root,
        manifest_url="https://example.test/JAR.json",
        source_timezone=ZoneInfo("Europe/Sofia"),
        maximum_frame_age=datetime.resolution,
        poll_interval_seconds=240,
        segmentation_minimum_dbz=35,
        segmentation_minimum_pixels=4,
        tracker_maximum_gap=datetime.resolution,
        tracker_maximum_speed_kmh=160,
    )


async def _get(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def _post(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path)


def test_archive_rejects_mutated_content_for_the_same_source_filename(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path)
    frame = RadarFrame(
        filename="JAR260825071742.CAP20Z8.png",
        url="https://example.test/JAR260825071742.CAP20Z8.png",
        observed_at=datetime(2026, 8, 25, 7, 17, 42, tzinfo=timezone.utc),
        display_timestamp="25.08.2026 07:17",
    )

    archived = store.archive_frame(frame, b"first", datetime.now(timezone.utc))
    same = store.archive_frame(frame, b"first", datetime.now(timezone.utc))

    assert archived.already_present is False
    assert same.already_present is True
    assert store.raw_path(frame).read_bytes() == b"first"


def test_frame_retention_removes_expired_frames_and_per_frame_outputs(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path)
    cutoff = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)

    def frame(filename: str, observed_at: datetime) -> RadarFrame:
        return RadarFrame(
            filename=filename,
            url=f"https://example.test/{filename}",
            observed_at=observed_at,
            display_timestamp=observed_at.isoformat(),
        )

    expired = cutoff - timedelta(seconds=1)
    retained = cutoff
    old_yarlovo = frame("old-yarlovo.png", expired)
    new_yarlovo = frame("new-yarlovo.png", retained)
    for item in (old_yarlovo, new_yarlovo):
        store.archive_frame(item, b"png", cutoff)
        store.write_calibration(item, {"width": 720})
        store.write_cells(item, [])

    old_opera = frame("old-opera.h5", expired)
    new_opera = frame("new-opera.h5", retained)
    for item in (old_opera, new_opera):
        store.archive_opera_frame(item, b"hdf5", cutoff)
        store.archive_opera_overlay(item, b"overlay", {"quantity": "DBZH"})
        store.write_opera_cells(item, [])

    old_volume = frame("old-volume.h5", expired)
    new_volume = frame("new-volume.h5", retained)
    for item in (old_volume, new_volume):
        store.archive_opera_volume_frame(item, b"volume", cutoff)

    store.prune_frames_older_than(cutoff)

    assert not store.raw_path(old_yarlovo).exists()
    assert not (store.metadata_dir / f"{old_yarlovo.filename}.json").exists()
    assert not (store.calibration_dir / f"{old_yarlovo.filename}.json").exists()
    assert not (store.cells_dir / f"{old_yarlovo.filename}.json").exists()
    assert store.raw_path(new_yarlovo).is_file()

    assert not store.opera_raw_path(old_opera).exists()
    assert not (store.opera_metadata_dir / f"{old_opera.filename}.json").exists()
    assert not store.opera_overlay_path(old_opera).exists()
    assert not (store.opera_overlays_dir / f"{old_opera.filename}.json").exists()
    assert not (store.opera_cells_dir / f"{old_opera.filename}.json").exists()
    assert store.opera_raw_path(new_opera).is_file()
    assert store.opera_overlay_path(new_opera).is_file()

    assert not store.opera_volume_raw_path(old_volume).exists()
    assert not (store.opera_volume_metadata_dir / f"{old_volume.filename}.json").exists()
    assert store.opera_volume_raw_path(new_volume).is_file()


def test_health_is_unavailable_until_a_fresh_poll_exists(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    response = asyncio.run(_get(app, "/health"))

    assert response.status_code == 503
    assert response.json()["state"] == "starting"


def test_manual_opera_fetch_reports_when_ingestion_is_disabled(tmp_path: Path) -> None:
    app = create_app(replace(_settings(tmp_path), opera_enabled=False))

    response = asyncio.run(_post(app, "/opera/fetch"))

    assert response.status_code == 409
    assert response.json()["detail"] == "OPERA ingestion is disabled"


def test_opera_readiness_is_primary_while_yarlovo_is_reported_as_secondary(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    archive = ArchiveStore(tmp_path)
    archive.write_status({"state": "stale", "error": "old Yarlovo CAPPI frame"})
    archive.write_opera_status(
        {
            "state": "ready",
            "latest_dbzh_composite": {"filename": "OPERA@20260825T1105@0@DBZH.h5"},
        }
    )
    app = create_app(settings)

    response = asyncio.run(_get(app, "/health"))

    assert response.status_code == 200
    assert response.json()["primary_source"] == "eumetnet_opera_dbzh_maximum_reflectivity"
    assert response.json()["yarlovo"]["state"] == "stale"


def test_dashboard_and_archived_frame_endpoints_are_available(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    archive = ArchiveStore(tmp_path)
    observed_now = datetime.now(timezone.utc)
    frame = RadarFrame(
        filename="JAR260825071742.CAP20Z8.png",
        url="https://example.test/JAR260825071742.CAP20Z8.png",
        observed_at=datetime(2026, 8, 25, 7, 17, 42, tzinfo=timezone.utc),
        display_timestamp="25.08.2026 07:17",
    )
    archive.archive_frame(frame, b"test-png-bytes", datetime.now(timezone.utc))
    archive.write_cells(frame, [{"cell_id": "cell-001"}])
    opera_frame = RadarFrame(
        filename="OPERA@20260825T1055@0@DBZH.h5",
        url="https://example.test/OPERA@20260825T1055@0@DBZH.h5",
        observed_at=observed_now,
        display_timestamp=observed_now.strftime("%d.%m.%Y %H:%M UTC"),
    )
    archive.archive_opera_overlay(opera_frame, b"opera-overlay", {"quantity": "DBZH"})
    archive.archive_opera_frame(opera_frame, b"opera-raw", datetime.now(timezone.utc))
    app = create_app(settings)

    dashboard = asyncio.run(_get(app, "/"))
    frame_response = asyncio.run(_get(app, f"/frames/{frame.filename}"))
    cells_response = asyncio.run(_get(app, f"/cells/{frame.filename}"))
    opera_response = asyncio.run(_get(app, f"/opera/overlay/{opera_frame.filename}"))
    history_response = asyncio.run(_get(app, "/opera/history"))
    volume_status_response = asyncio.run(_get(app, "/opera/volume/status"))

    assert dashboard.status_code == 200
    assert "Hail detector" in dashboard.text
    assert frame_response.status_code == 200
    assert frame_response.content == b"test-png-bytes"
    assert cells_response.json()["cells"] == [{"cell_id": "cell-001"}]
    assert opera_response.status_code == 200
    assert opera_response.content == b"opera-overlay"
    assert history_response.status_code == 200
    assert history_response.json()["frames"] == [
        {
            "filename": opera_frame.filename,
            "observed_at": opera_frame.source_timestamp,
            "display_timestamp": opera_frame.display_timestamp,
        }
    ]
    assert volume_status_response.status_code == 200
    assert volume_status_response.json()["experimental"] is True
