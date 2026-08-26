from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import h5py
import httpx
import numpy as np

from hail_detector.archive import ArchiveStore
from hail_detector.config import Settings
from hail_detector.opera_volume import (
    OperaVolumeClient,
    OperaVolumeIngestionService,
    extract_opera_volume_profile,
    parse_opera_volume_frames,
)


VOLUME_FILE = "rocra@20260825T1055@0.5_1.5@DBZH.h5"
VOLUME_URL = f"https://files.example.test/{VOLUME_FILE}"


def _settings(root: Path) -> Settings:
    return Settings(
        archive_root=root,
        manifest_url="https://example.test/JAR.json",
        source_timezone=ZoneInfo("Europe/Sofia"),
        maximum_frame_age=timedelta(minutes=10),
        poll_interval_seconds=240,
        segmentation_minimum_dbz=35,
        segmentation_minimum_pixels=4,
        tracker_maximum_gap=timedelta(minutes=12),
        tracker_maximum_speed_kmh=160,
        opera_volume_lookback=timedelta(minutes=15),
        opera_volume_maximum_frame_age=timedelta(minutes=15),
    )


def _coverage_payload() -> dict[str, object]:
    return {
        "type": "CoverageCollection",
        "coverages": [
            {
                "links": [
                    {"rel": "items", "type": "application/x-odim", "href": VOLUME_URL},
                    {
                        "rel": "items",
                        "type": "application/x-odim",
                        "href": "https://files.example.test/rocra@20260825T1055@0.5@VRADH.h5",
                    },
                ]
            }
        ],
    }


def _volume_hdf5() -> bytes:
    encoded = BytesIO()
    with h5py.File(encoded, "w") as file:
        root_what = file.create_group("what")
        root_what.attrs["object"] = b"PVOL"
        where = file.create_group("where")
        where.attrs["lat"] = 42.0
        where.attrs["lon"] = 23.0
        where.attrs["height"] = 300.0
        for index, elevation in enumerate((0.5, 1.5), start=1):
            dataset = file.create_group(f"dataset{index}")
            dataset_what = dataset.create_group("what")
            dataset_what.attrs["product"] = b"SCAN"
            dataset_where = dataset.create_group("where")
            dataset_where.attrs["elangle"] = elevation
            dataset_where.attrs["nrays"] = 360
            dataset_where.attrs["nbins"] = 100
            dataset_where.attrs["rscale"] = 1_000.0
            dataset_where.attrs["rstart"] = 0.0
            dataset_where.attrs["a1gate"] = 0
            how = dataset.create_group("how")
            how.attrs["startazA"] = np.arange(360, dtype=np.float64)
            data_group = dataset.create_group("data1")
            data_what = data_group.create_group("what")
            data_what.attrs["quantity"] = b"DBZH"
            data_what.attrs["gain"] = 0.5
            data_what.attrs["offset"] = -32.0
            data_what.attrs["nodata"] = 255.0
            data_what.attrs["undetect"] = 0.0
            data = data_group.create_dataset("data", data=np.zeros((360, 100), dtype=np.uint8))
            # The requested point is roughly 56 km due north. Set the local
            # three-ray / three-gate neighbourhood to 48 dBZ after decoding.
            for ray in (359, 0, 1):
                data[ray, 55:58] = 160
    return encoded.getvalue()


def _track_state() -> dict[str, object]:
    point = {
        "observed_at": "2026-08-25T10:55:00+00:00",
        "centroid_location": {"latitude": 42.5, "longitude": 23.0},
    }
    return {"tracks": [{"track_id": "track-001", "points": [point, point]}]}


def test_volume_parser_accepts_nested_dbzh_links_with_minute_timestamps() -> None:
    frames = parse_opera_volume_frames(_coverage_payload())

    assert [frame.filename for frame in frames] == [VOLUME_FILE]
    assert frames[0].observed_at == datetime(2026, 8, 25, 10, 55, tzinfo=timezone.utc)


def test_volume_profile_reports_local_maximum_by_elevation() -> None:
    profile = extract_opera_volume_profile(
        _volume_hdf5(),
        latitude=42.5,
        longitude=23.0,
        maximum_beam_height_metres=20_000,
    )

    assert profile["scan_count"] == 2
    assert profile["distance_km"] > 50
    assert [sample["max_dbzh"] for sample in profile["profile"]] == [48.0, 48.0]
    assert profile["profile"][1]["beam_height_metres_msl"] > profile["profile"][0]["beam_height_metres_msl"]


def test_volume_service_archives_and_profiles_confirmed_primary_tracks(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.example.test":
            assert request.url.params["standard_name"] == "DBZH"
            assert request.url.params["method"] == "scan"
            return httpx.Response(200, json=_coverage_payload())
        if str(request.url) == VOLUME_URL:
            return httpx.Response(200, content=_volume_hdf5())
        return httpx.Response(404)

    settings = _settings(tmp_path)
    archive = ArchiveStore(tmp_path)
    archive.write_opera_tracker_state(_track_state())
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OperaVolumeClient("https://api.example.test/volume", settings.opera_volume_lookback, http_client)
        result = OperaVolumeIngestionService(settings, archive, client).poll_once(
            datetime(2026, 8, 25, 11, 0, tzinfo=timezone.utc)
        )

    assert result.state == "ready"
    assert result.archived_frames == 1
    assert result.track_profiles[0]["track_id"] == "track-001"
    assert result.track_profiles[0]["profile"][0]["max_dbzh"] == 48.0
    assert archive.opera_volume_raw_path(result.latest_frame).is_file()  # type: ignore[arg-type]
    status = archive.read_opera_volume_status()
    assert status is not None
    assert status["experimental"] is True
    assert status["not_used_for"] == ["tracking", "trajectory", "hail alerts"]
