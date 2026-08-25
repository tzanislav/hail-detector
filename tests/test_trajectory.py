from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hail_detector.config import TargetLocation
from hail_detector.geospatial import LatLon, PixelCoordinate
from hail_detector.tracking import StormTrack, TrackPoint
from hail_detector.trajectory import project_track


TIMEZONE = ZoneInfo("Europe/Sofia")
TARGET = TargetLocation(latitude=42.6977, longitude=23.3219, warning_radius_km=5)


def _point(observed_at: datetime, latitude: float, longitude: float) -> TrackPoint:
    return TrackPoint(
        observed_at=observed_at,
        cell_id=f"cell-{observed_at.strftime('%H%M%S')}",
        centroid_pixel=PixelCoordinate(300, 300),
        centroid_location=LatLon(latitude, longitude),
        pixel_count=10,
        area_square_km=3.0,
        area_at_or_above_square_km={},
        max_dbz_lower=45,
        max_dbz_upper=50,
    )


def test_projection_returns_arrival_window_when_track_heads_through_target() -> None:
    first_time = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    track = StormTrack(
        "track-000001",
        [
            _point(first_time, 42.6977, 23.10),
            _point(first_time + timedelta(minutes=10), 42.6977, 23.20),
            _point(first_time + timedelta(minutes=20), 42.6977, 23.30),
        ],
    )

    forecast = project_track(track, TARGET, timedelta(minutes=30))

    assert forecast.available is True
    assert forecast.confidence == "medium"
    assert forecast.intersects_target_radius is True
    assert forecast.speed_kmh == pytest.approx(48.9, abs=1.0)
    assert forecast.arrival_window_start is not None
    assert forecast.arrival_window_end is not None
    assert forecast.arrival_window_start == first_time + timedelta(minutes=20)
    assert forecast.arrival_window_end > forecast.arrival_window_start
    assert forecast.closest_approach_distance_km == pytest.approx(0, abs=0.01)


def test_projection_rejects_track_moving_away_from_target() -> None:
    first_time = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    track = StormTrack(
        "track-000002",
        [
            _point(first_time, 42.6977, 23.20),
            _point(first_time + timedelta(minutes=10), 42.6977, 23.10),
            _point(first_time + timedelta(minutes=20), 42.6977, 23.00),
        ],
    )

    forecast = project_track(track, TARGET, timedelta(minutes=30))

    assert forecast.available is True
    assert forecast.intersects_target_radius is False
    assert forecast.closest_approach_at == first_time + timedelta(minutes=20)
    assert forecast.arrival_window_start is None


def test_projection_requires_three_observations() -> None:
    observed_at = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    track = StormTrack(
        "track-000003",
        [
            _point(observed_at, 42.6977, 23.20),
            _point(observed_at + timedelta(minutes=10), 42.6977, 23.10),
        ],
    )

    forecast = project_track(track, TARGET, timedelta(minutes=30))

    assert forecast.available is False
    assert forecast.confidence == "low"
    assert forecast.reason == "at least 3 observations are required before trajectory prediction"


def test_projection_serializes_half_hour_hour_and_two_hour_positions() -> None:
    first_time = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    track = StormTrack(
        "track-000004",
        [
            _point(first_time, 42.6977, 23.10),
            _point(first_time + timedelta(minutes=10), 42.6977, 23.20),
            _point(first_time + timedelta(minutes=20), 42.6977, 23.30),
        ],
    )

    forecast = project_track(track, TARGET, timedelta(minutes=120))

    assert [item.lead_minutes for item in forecast.predicted_positions] == [30, 60, 120]
    assert forecast.predicted_positions[0].predicted_at == first_time + timedelta(minutes=50)
    assert forecast.predicted_positions[-1].location.longitude > forecast.predicted_positions[0].location.longitude
    serialized = forecast.as_dict()
    assert serialized["predicted_positions"][2]["lead_minutes"] == 120
