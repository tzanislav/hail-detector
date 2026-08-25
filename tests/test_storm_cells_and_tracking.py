from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image
import pytest

from hail_detector.calibration import DbzBand
from hail_detector.geospatial import PixelCoordinate, YARLOVO_BOUNDS
from hail_detector.models import RadarFrame
from hail_detector.storm_cells import PixelBounds, StormCell, segment_cells
from hail_detector.tracking import TrackManager


TIMEZONE = ZoneInfo("Europe/Sofia")


def _frame(name: str, observed_at: datetime) -> RadarFrame:
    return RadarFrame(
        filename=name,
        url=f"https://example.test/{name}",
        observed_at=observed_at,
        display_timestamp=observed_at.strftime("%d.%m.%Y %H:%M"),
    )


def _png_with_two_cores() -> bytes:
    image = Image.new("RGBA", (720, 720), (0, 0, 0, 0))
    for column in (10, 11):
        for row in (20, 21):
            image.putpixel((column, row), (255, 255, 30, 255))  # 35–40 dBZ
    for column in (100, 101):
        for row in (200, 201):
            image.putpixel((column, row), (237, 0, 0, 255))  # 45–50 dBZ
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    return encoded.getvalue()


def _cell(frame: RadarFrame, column: float, row: float, *, lower_dbz: int = 35) -> StormCell:
    upper_dbz = lower_dbz + 5
    return StormCell(
        cell_id=f"{frame.filename}#{int(column)}",
        frame_filename=frame.filename,
        observed_at=frame.observed_at,
        centroid_pixel=PixelCoordinate(column=column, row=row),
        centroid_location=YARLOVO_BOUNDS.pixel_to_latlon(column, row),
        pixel_count=4,
        area_square_km=4
        * YARLOVO_BOUNDS.ground_resolution_at(column, row)[0]
        * YARLOVO_BOUNDS.ground_resolution_at(column, row)[1]
        / 1_000_000,
        area_at_or_above_square_km={f">={lower_dbz}": 1.0},
        max_band=DbzBand(f"{lower_dbz}-{upper_dbz}", lower_dbz, upper_dbz),
        band_pixel_counts={f"{lower_dbz}-{upper_dbz}": 4},
        pixel_bounds=PixelBounds(
            min_column=int(column),
            min_row=int(row),
            max_column=int(column) + 1,
            max_row=int(row) + 1,
        ),
    )


def test_segment_cells_uses_8_connected_components_and_georeferences_centroids() -> None:
    observed_at = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    frame = _frame("JAR260825100000.CAPTEST.png", observed_at)

    cells = segment_cells(
        _png_with_two_cores(), frame, minimum_dbz=35, minimum_pixels=4
    )

    assert [cell.pixel_count for cell in cells] == [4, 4]
    assert [cell.max_band.label for cell in cells] == ["35-40", "45-50"]
    assert cells[0].centroid_pixel == PixelCoordinate(column=10.5, row=20.5)
    assert cells[0].centroid_location.latitude > cells[1].centroid_location.latitude
    assert cells[0].area_square_km == pytest.approx(1.31, abs=0.02)


def test_tracker_preserves_id_and_derives_motion_and_intensity_trend() -> None:
    first_time = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    second_time = first_time + timedelta(minutes=4)
    first_frame = _frame("JAR260825100000.CAPTEST.png", first_time)
    second_frame = _frame("JAR260825100400.CAPTEST.png", second_time)
    manager = TrackManager(maximum_gap=timedelta(minutes=12), maximum_speed_kmh=160)

    initial = manager.associate_frame(first_time, [_cell(first_frame, 300, 300)])
    next_result = manager.associate_frame(
        second_time, [_cell(second_frame, 305, 302, lower_dbz=40)]
    )
    track = manager.tracks[initial.new_track_ids[0]]
    summary = manager.track_summary(track)

    assert next_result.matched_cell_ids == ("JAR260825100400.CAPTEST.png#305",)
    assert next_result.new_track_ids == ()
    assert len(track.points) == 2
    assert summary["motion_confidence"] == "medium"
    assert summary["speed_kmh"] == pytest.approx(47.0, abs=1.5)
    assert summary["bearing_degrees"] is not None
    assert summary["intensity_trend"] == "strengthening"


def test_tracker_state_round_trip_retains_next_track_number() -> None:
    observed_at = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    frame = _frame("JAR260825100000.CAPTEST.png", observed_at)
    manager = TrackManager(maximum_gap=timedelta(minutes=12), maximum_speed_kmh=160)
    manager.associate_frame(observed_at, [_cell(frame, 300, 300)])

    restored = TrackManager.from_dict(
        manager.as_dict(), maximum_gap=timedelta(minutes=12), maximum_speed_kmh=160
    )

    assert restored.last_processed_at == observed_at
    assert restored.next_track_number == 2
    assert restored.tracks["track-000001"].latest.cell_id.endswith("#300")


def test_tracker_retires_echoes_after_the_configured_gap() -> None:
    observed_at = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    frame = _frame("JAR260825100000.CAPTEST.png", observed_at)
    manager = TrackManager(maximum_gap=timedelta(minutes=12), maximum_speed_kmh=160)
    created = manager.associate_frame(observed_at, [_cell(frame, 300, 300)])

    retired = manager.prune_stale_tracks(observed_at + timedelta(minutes=13))

    assert retired == created.new_track_ids
    assert manager.tracks == {}
