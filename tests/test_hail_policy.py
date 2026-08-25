from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hail_detector.config import HailPolicySettings
from hail_detector.geospatial import LatLon, PixelCoordinate
from hail_detector.hail_policy import assess_track
from hail_detector.tracking import StormTrack, TrackPoint
from hail_detector.trajectory import TrajectoryForecast


TIMEZONE = ZoneInfo("Europe/Sofia")


def _track(maximum_dbz: int, previous_dbz: int) -> StormTrack:
    observed_at = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    previous = TrackPoint(
        observed_at=observed_at - timedelta(minutes=4),
        cell_id="previous-cell",
        centroid_pixel=PixelCoordinate(300, 300),
        centroid_location=LatLon(42.7, 23.1),
        pixel_count=20,
        area_square_km=8.0,
        area_at_or_above_square_km={">=55": 6.0, ">=60": 6.0},
        max_dbz_lower=previous_dbz,
        max_dbz_upper=previous_dbz + 5,
    )
    latest = TrackPoint(
        observed_at=observed_at,
        cell_id="latest-cell",
        centroid_pixel=PixelCoordinate(305, 302),
        centroid_location=LatLon(42.7, 23.2),
        pixel_count=20,
        area_square_km=8.0,
        area_at_or_above_square_km={">=55": 6.0, ">=60": 6.0},
        max_dbz_lower=maximum_dbz,
        max_dbz_upper=maximum_dbz + 5,
    )
    return StormTrack("track-000001", [previous, latest])


def _intersecting_forecast(minutes_to_arrival: int) -> TrajectoryForecast:
    observed_at = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    arrival = observed_at + timedelta(minutes=minutes_to_arrival)
    return TrajectoryForecast(
        track_id="track-000001",
        available=True,
        confidence="medium",
        reason=None,
        current_distance_km=30.0,
        speed_kmh=55.0,
        bearing_degrees=90.0,
        intersects_target_radius=True,
        closest_approach_distance_km=1.0,
        closest_approach_at=arrival + timedelta(minutes=5),
        arrival_window_start=arrival,
        arrival_window_end=arrival + timedelta(minutes=10),
    )


def test_calibrated_likely_hail_core_can_produce_warning_candidate() -> None:
    assessment = assess_track(
        _track(60, 55),
        _intersecting_forecast(30),
        HailPolicySettings(high_dbz_calibration_validated=True),
    )

    assert assessment.radar_signature == "likely_hail_signature"
    assert assessment.candidate_level == "Hail Warning"
    assert assessment.confidence == "high"
    assert assessment.high_dbz_core_area_square_km == 6.0


def test_possible_hail_core_can_produce_watch_candidate() -> None:
    assessment = assess_track(
        _track(55, 55),
        _intersecting_forecast(60),
        HailPolicySettings(high_dbz_calibration_validated=True),
    )

    assert assessment.radar_signature == "possible_hail_signature"
    assert assessment.candidate_level == "Hail Watch"


def test_uncalibrated_high_dbz_policy_suppresses_candidate() -> None:
    assessment = assess_track(
        _track(60, 55),
        _intersecting_forecast(30),
        HailPolicySettings(high_dbz_calibration_validated=False),
    )

    assert assessment.candidate_level is None
    assert assessment.calibration_gate_open is False
    assert any("candidate suppressed" in reason for reason in assessment.reasons)
