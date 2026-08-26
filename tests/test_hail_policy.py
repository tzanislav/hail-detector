from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hail_detector.config import SevereAlertSettings
from hail_detector.geospatial import LatLon, PixelCoordinate
from hail_detector.hail_policy import SEVERE_ALERT_LEVEL, assess_track
from hail_detector.tracking import StormTrack, TrackPoint
from hail_detector.trajectory import TrajectoryForecast


TIMEZONE = ZoneInfo("Europe/Sofia")


def _track(maximum_dbz: int) -> StormTrack:
    observed_at = datetime(2026, 8, 26, 10, 0, tzinfo=TIMEZONE)
    point = TrackPoint(
        observed_at=observed_at,
        cell_id="cell-001",
        centroid_pixel=PixelCoordinate(300, 300),
        centroid_location=LatLon(42.7, 23.2),
        pixel_count=8,
        area_square_km=8.0,
        area_at_or_above_square_km={">=65": 2.0},
        max_dbz_lower=maximum_dbz,
        max_dbz_upper=maximum_dbz + 5,
    )
    return StormTrack("track-000001", [point, point, point])


def _forecast(minutes_to_arrival: int, confidence: str = "medium") -> TrajectoryForecast:
    observed_at = datetime(2026, 8, 26, 10, 0, tzinfo=TIMEZONE)
    arrival = observed_at + timedelta(minutes=minutes_to_arrival)
    return TrajectoryForecast(
        track_id="track-000001",
        available=True,
        confidence=confidence,
        reason=None,
        current_distance_km=20.0,
        speed_kmh=50.0,
        bearing_degrees=90.0,
        intersects_target_radius=True,
        closest_approach_distance_km=1.0,
        closest_approach_at=arrival,
        arrival_window_start=arrival,
        arrival_window_end=arrival + timedelta(minutes=5),
    )


def test_65dbz_opera_track_entering_within_thirty_minutes_is_a_candidate() -> None:
    assessment = assess_track(_track(65), _forecast(30), SevereAlertSettings())

    assert assessment.candidate_level == SEVERE_ALERT_LEVEL
    assert assessment.source == "eumetnet_opera_dbzh_maximum_reflectivity"


def test_severe_signature_is_suppressed_when_outside_the_lead_window() -> None:
    assessment = assess_track(_track(65), _forecast(31), SevereAlertSettings())

    assert assessment.candidate_level is None
    assert any("outside the 30-minute" in reason for reason in assessment.reasons)


def test_severe_signature_requires_the_configured_motion_confidence() -> None:
    assessment = assess_track(_track(65), _forecast(10, confidence="low"), SevereAlertSettings())

    assert assessment.candidate_level is None
    assert any("trajectory confidence" in reason for reason in assessment.reasons)
