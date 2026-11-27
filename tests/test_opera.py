from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path

import h5py
import httpx

from hail_detector.archive import ArchiveStore
from hail_detector.config import Settings, TelegramSettings
from hail_detector.geospatial import LatLon, PixelCoordinate
from hail_detector.hail_policy import HailAssessment, SEVERE_ALERT_LEVEL
from hail_detector.opera import (
    OperaAnalysisResult,
    OperaClient,
    OperaIngestionService,
    parse_opera_composite_frames,
    qualify_opera_storm_cell,
    segment_opera_cells,
)
from hail_detector.storm_cells import DbzBand, PixelBounds, StormCell
from hail_detector.telegram import TelegramNotifier


OPERA_FILE = "OPERA@20260825T1055@0@DBZH.h5"
OPERA_URL = f"https://files.example.test/{OPERA_FILE}"


def _settings(root: Path) -> Settings:
    return Settings(
        archive_root=root,
        poll_interval_seconds=240,
        tracker_maximum_gap=timedelta(minutes=12),
        tracker_maximum_speed_kmh=160,
        opera_lookback=timedelta(minutes=70),
        opera_maximum_frame_age=timedelta(minutes=15),
    )


def _coverage_payload() -> dict[str, object]:
    return {
        "type": "Coverage",
        "links": [
            {"rel": "items", "type": "application/x-odim", "href": OPERA_URL},
            {
                "rel": "items",
                "type": "application/x-odim",
                "href": "https://files.example.test/OPERA@20260825T1055@0@RATE.h5",
            },
            {"rel": "canonical", "type": "text/html", "href": "https://eumetnet.example.test"},
        ],
    }


def _opera_hdf5(*, embedded_intense_core: bool = False) -> bytes:
    """A sparse, real-geometry ODIM file with a high-reflectivity Sofia core."""

    encoded = BytesIO()
    with h5py.File(encoded, "w") as file:
        root_what = file.create_group("what")
        root_what.attrs["date"] = b"20260825"
        root_what.attrs["time"] = b"105500"
        root_what.attrs["object"] = b"COMP"
        where = file.create_group("where")
        where.attrs["projdef"] = (
            b"+proj=laea +lat_0=55.0 +lon_0=10.0 +x_0=1950000.0 "
            b"+y_0=-2100000.0 +units=m +ellps=WGS84"
        )
        where.attrs["xscale"] = 1000.0
        where.attrs["yscale"] = 1000.0
        where.attrs["xsize"] = 3800
        where.attrs["ysize"] = 4400
        dataset = file.create_group("dataset1")
        dataset_what = dataset.create_group("what")
        dataset_what.attrs["product"] = b"MAX"
        data_group = dataset.create_group("data1")
        data_what = data_group.create_group("what")
        data_what.attrs["quantity"] = b"DBZH"
        data_what.attrs["gain"] = 1.0
        data_what.attrs["offset"] = 0.0
        data_what.attrs["nodata"] = -9999000.0
        data_what.attrs["undetect"] = -8888000.0
        data = data_group.create_dataset(
            "data",
            shape=(4400, 3800),
            dtype="f4",
            chunks=(100, 100),
            compression="gzip",
            fillvalue=-9999000.0,
        )
        # These grid cells map into the configured Sofia overlay bounds.
        data[3350:3380, 3020:3060] = 35.0 if embedded_intense_core else 64.0
        if embedded_intense_core:
            # Keep the component connected while placing its strongest core
            # toward the south-east edge of the broad weak echo.
            data[3360:3370, 3050:3060] = 64.0
    return encoded.getvalue()


def _cell(*, area: float, core_45: float, core_55: float) -> StormCell:
    observed_at = datetime(2026, 8, 25, 10, 55, tzinfo=timezone.utc)
    return StormCell(
        cell_id="synthetic#001",
        frame_filename=OPERA_FILE,
        observed_at=observed_at,
        centroid_pixel=PixelCoordinate(column=100, row=100),
        centroid_location=LatLon(latitude=42.7, longitude=23.3),
        pixel_count=max(1, int(area)),
        area_square_km=area,
        area_at_or_above_square_km={">=35": area, ">=45": core_45, ">=55": core_55},
        max_band=DbzBand("55-60" if core_55 else "40-45", 55 if core_55 else 40, 60 if core_55 else 45),
        band_pixel_counts={},
        pixel_bounds=PixelBounds(min_column=100, min_row=100, max_column=100, max_row=100),
    )


def test_parser_accepts_only_timestamped_opera_dbzh_hdf5_links() -> None:
    frames = parse_opera_composite_frames(_coverage_payload())

    assert [frame.filename for frame in frames] == [OPERA_FILE]
    assert frames[0].observed_at == datetime(2026, 8, 25, 10, 55, tzinfo=timezone.utc)


def test_opera_client_sends_the_api_key_as_a_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "test-opera-key"
        assert "apikey" not in request.url.params
        return httpx.Response(200, json=_coverage_payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        snapshot = OperaClient(
            "https://api.example.test/opera",
            timedelta(minutes=70),
            http_client,
            api_key="test-opera-key",
        ).fetch_snapshot(datetime(2026, 8, 25, 11, 0, tzinfo=timezone.utc))

    assert snapshot.frames[0].filename == OPERA_FILE


def test_opera_qualification_rejects_weak_fragments_but_keeps_intense_cores(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    weak, reason = qualify_opera_storm_cell(_cell(area=23, core_45=0, core_55=0), settings)
    broad, _ = qualify_opera_storm_cell(_cell(area=90, core_45=12, core_55=0), settings)
    compact, _ = qualify_opera_storm_cell(_cell(area=5, core_45=2, core_55=1), settings)

    assert not weak
    assert "below operational storm gate" in reason
    assert broad
    assert compact


def test_opera_centroid_is_cubically_weighted_toward_the_strongest_core() -> None:
    frame = parse_opera_composite_frames(_coverage_payload())[0]

    cells = segment_opera_cells(
        _opera_hdf5(embedded_intense_core=True),
        frame,
        minimum_dbz=35,
        minimum_pixels=2,
        centroid_weight_exponent=3,
    )

    assert len(cells) == 1
    # The geometric centre would be column 3039.5 / row 3364.5. The 64 dBZ
    # core is at the east edge, and cubic excess weighting pulls the centroid
    # substantially toward it.
    assert cells[0].centroid_pixel.column > 3053


def test_opera_service_archives_and_renders_a_comparison_overlay(tmp_path: Path) -> None:
    coverage = _coverage_payload()
    hdf5 = _opera_hdf5()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.example.test":
            assert request.url.params["standard_name"] == "DBZH"
            assert request.url.params["method"] == "comp"
            return httpx.Response(200, json=coverage)
        if str(request.url) == OPERA_URL:
            return httpx.Response(200, content=hdf5, headers={"content-type": "application/x-hdf5"})
        return httpx.Response(404)

    settings = _settings(tmp_path)
    archive = ArchiveStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OperaClient("https://api.example.test/opera", settings.opera_lookback, http_client)
        result = OperaIngestionService(settings, archive, client).poll_once(
            datetime(2026, 8, 25, 11, 0, tzinfo=timezone.utc)
        )

    assert result.state == "ready"
    assert result.archived_frames == 1
    assert result.analyzed_frames == 1
    assert result.segmented_cells == 1
    assert result.qualified_storm_cells == 1
    assert result.active_tracks == 0  # one frame is not yet a confirmed track
    assert archive.opera_raw_path(result.latest_frame).is_file()  # type: ignore[arg-type]
    assert archive.opera_overlay_path(result.latest_frame).is_file()  # type: ignore[arg-type]
    status = archive.read_opera_status()
    assert status is not None
    assert status["latest_dbzh_composite"]["filename"] == OPERA_FILE
    summary_path = archive.opera_overlays_dir / f"{OPERA_FILE}.json"
    assert '"maximum_dbzh": 64.0' in summary_path.read_text(encoding="utf-8")
    assert '"source_observed_timestamp": "20260825T105500"' in summary_path.read_text(
        encoding="utf-8"
    )
    cells_path = archive.opera_cells_dir / f"{OPERA_FILE}.json"
    assert '"area_square_km": 1200.0' in cells_path.read_text(encoding="utf-8")
    assert '"rejected_cells": []' in cells_path.read_text(encoding="utf-8")


def test_opera_service_delivers_a_fresh_severe_alert(tmp_path: Path) -> None:
    coverage = _coverage_payload()
    hdf5 = _opera_hdf5()
    sent_payloads: list[dict[str, object]] = []

    def opera_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.example.test":
            return httpx.Response(200, json=coverage)
        if str(request.url) == OPERA_URL:
            return httpx.Response(200, content=hdf5)
        return httpx.Response(404)

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    class AlertingAnalysis:
        def process(self, _frames):
            assessment = HailAssessment(
                source="eumetnet_opera_dbzh_maximum_reflectivity",
                track_id="track-000001",
                radar_signature="severe_reflectivity_signature",
                confidence="high",
                candidate_level=SEVERE_ALERT_LEVEL,
                max_dbz_band=(65, 70),
                projected_target_intersection=True,
                minutes_to_arrival=20,
                reasons=("qualifying test assessment",),
            )
            return OperaAnalysisResult(
                processed_frames=1,
                segmented_cells=1,
                qualified_storm_cells=1,
                active_tracks=1,
                forecasted_tracks=1,
                intersecting_target_tracks=1,
                earliest_intersection_at="2026-08-25T11:15:00+00:00",
                assessments=(assessment,),
                errors=(),
            )

    settings = _settings(tmp_path)
    archive = ArchiveStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(opera_handler)) as opera_http:
        client = OperaClient("https://api.example.test/opera", settings.opera_lookback, opera_http)
        with httpx.Client(transport=httpx.MockTransport(telegram_handler)) as telegram_http:
            notifier = TelegramNotifier(
                TelegramSettings(enabled=True, bot_token="test-token", chat_id="12345"),
                telegram_http,
            )
            result = OperaIngestionService(
                settings, archive, client, analysis=AlertingAnalysis(), telegram_notifier=notifier
            ).poll_once(datetime(2026, 8, 25, 11, 0, tzinfo=timezone.utc))

    assert result.severe_alert_candidates == 1
    assert result.telegram_deliveries == 1
    assert sent_payloads[0]["chat_id"] == "12345"
    assert "Severe Reflectivity Alert" in str(sent_payloads[0]["text"])
