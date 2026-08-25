from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hail_detector.geospatial import LatLon, PixelCoordinate
from hail_detector.motion import estimate_track_motion
from hail_detector.tracking import StormTrack, TrackPoint


def _point(observed_at: datetime, latitude: float, longitude: float) -> TrackPoint:
    return TrackPoint(
        observed_at=observed_at,
        cell_id=f"cell-{observed_at:%H%M}",
        centroid_pixel=PixelCoordinate(0, 0),
        centroid_location=LatLon(latitude, longitude),
        pixel_count=1,
        area_square_km=1.0,
        area_at_or_above_square_km={},
        max_dbz_lower=45,
        max_dbz_upper=50,
    )


def test_recency_weighted_motion_fit_smooths_centroid_jitter_and_keeps_latest_position() -> None:
    start = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
    track = StormTrack(
        "track-000001",
        [
            _point(start, 42.700, 23.000),
            _point(start + timedelta(minutes=5), 42.705, 23.130),
            _point(start + timedelta(minutes=10), 42.692, 23.260),
            _point(start + timedelta(minutes=15), 42.707, 23.390),
            _point(start + timedelta(minutes=20), 42.699, 23.520),
        ],
    )

    estimate = estimate_track_motion(track, window_observations=5)

    assert estimate.available is True
    assert estimate.confidence == "high"
    assert estimate.points_used == 5
    assert estimate.speed_kmh == pytest.approx(127, abs=8)
    assert estimate.bearing_degrees == pytest.approx(90, abs=6)
    assert estimate.residual_km is not None and estimate.residual_km > 0.2
    assert estimate.smoothed_points[-1].centroid_location == track.latest.centroid_location
    # The central raw centroid is south of the fitted path; it remains in the
    # raw audit history but no longer introduces a bend in the displayed path.
    assert estimate.smoothed_points[2].centroid_location.latitude > 42.697
