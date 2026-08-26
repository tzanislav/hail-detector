from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from hail_detector.api import create_app
from hail_detector.archive import ArchiveStore
from hail_detector.config import Settings
from hail_detector.models import RadarFrame


def _settings(root: Path) -> Settings:
    return Settings(
        archive_root=root,
        poll_interval_seconds=240,
        tracker_maximum_gap=timedelta(minutes=12),
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


def _frame(filename: str, observed_at: datetime) -> RadarFrame:
    return RadarFrame(
        filename=filename,
        url=f"https://example.test/{filename}",
        observed_at=observed_at,
        display_timestamp=observed_at.isoformat(),
    )


def test_archive_rejects_mutated_opera_content_for_the_same_filename(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path)
    frame = _frame("OPERA@20260826T1200@0@DBZH.h5", datetime.now(timezone.utc))

    archived = store.archive_opera_frame(frame, b"first", datetime.now(timezone.utc))
    same = store.archive_opera_frame(frame, b"first", datetime.now(timezone.utc))

    assert archived.already_present is False
    assert same.already_present is True
    assert store.opera_raw_path(frame).read_bytes() == b"first"


def test_frame_retention_removes_expired_opera_and_volume_artifacts(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path)
    cutoff = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
    expired = cutoff - timedelta(seconds=1)
    retained = cutoff
    old_opera = _frame("old-opera.h5", expired)
    new_opera = _frame("new-opera.h5", retained)
    for item in (old_opera, new_opera):
        store.archive_opera_frame(item, b"hdf5", cutoff)
        store.archive_opera_overlay(item, b"overlay", {"quantity": "DBZH"})
        store.write_opera_cells(item, [])
    old_volume = _frame("old-volume.h5", expired)
    new_volume = _frame("new-volume.h5", retained)
    for item in (old_volume, new_volume):
        store.archive_opera_volume_frame(item, b"volume", cutoff)

    store.prune_frames_older_than(cutoff)

    assert not store.opera_raw_path(old_opera).exists()
    assert not store.opera_overlay_path(old_opera).exists()
    assert store.opera_raw_path(new_opera).is_file()
    assert store.opera_overlay_path(new_opera).is_file()
    assert not store.opera_volume_raw_path(old_volume).exists()
    assert store.opera_volume_raw_path(new_volume).is_file()


def test_health_is_unavailable_until_a_fresh_opera_poll_exists(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    response = asyncio.run(_get(app, "/health"))

    assert response.status_code == 503
    assert response.json()["state"] == "starting"


def test_manual_opera_fetch_reports_when_ingestion_is_disabled(tmp_path: Path) -> None:
    app = create_app(replace(_settings(tmp_path), opera_enabled=False))

    response = asyncio.run(_post(app, "/opera/fetch"))

    assert response.status_code == 409
    assert response.json()["detail"] == "OPERA ingestion is disabled"


def test_opera_readiness_and_alert_state_are_exposed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    archive = ArchiveStore(tmp_path)
    archive.write_opera_status({"state": "ready"})
    archive.write_alert_state({"records": [{"track_id": "track-001"}]})
    app = create_app(settings)

    health = asyncio.run(_get(app, "/health"))
    alerts = asyncio.run(_get(app, "/alerts"))

    assert health.status_code == 200
    assert health.json()["primary_source"] == "eumetnet_opera_dbzh_maximum_reflectivity"
    assert alerts.json()["records"] == [{"track_id": "track-001"}]


def test_dashboard_and_opera_archive_endpoints_are_available(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    archive = ArchiveStore(tmp_path)
    observed_now = datetime.now(timezone.utc)
    frame = _frame("OPERA@20260826T1200@0@DBZH.h5", observed_now)
    archive.archive_opera_overlay(frame, b"opera-overlay", {"quantity": "DBZH"})
    archive.archive_opera_frame(frame, b"opera-raw", observed_now)
    archive.write_opera_cells(frame, [{"cell_id": "cell-001"}])
    app = create_app(settings)

    dashboard = asyncio.run(_get(app, "/"))
    overlay = asyncio.run(_get(app, f"/opera/overlay/{frame.filename}"))
    cells = asyncio.run(_get(app, f"/opera/cells/{frame.filename}"))
    history = asyncio.run(_get(app, "/opera/history"))

    assert dashboard.status_code == 200
    assert "Hail detector" in dashboard.text
    assert overlay.content == b"opera-overlay"
    assert cells.json()["cells"] == [{"cell_id": "cell-001"}]
    assert history.json()["frames"] == [
        {
            "filename": frame.filename,
            "observed_at": frame.source_timestamp,
            "display_timestamp": frame.display_timestamp,
        }
    ]
