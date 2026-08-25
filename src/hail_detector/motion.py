from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from math import atan2, cos, degrees, radians, sqrt
from typing import TYPE_CHECKING

from .geospatial import LatLon


if TYPE_CHECKING:
    from .tracking import StormTrack


EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True)
class SmoothedTrackPoint:
    """A fitted historical position, anchored to the latest raw observation."""

    observed_at: datetime
    centroid_location: LatLon

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["observed_at"] = self.observed_at.isoformat()
        return data


@dataclass(frozen=True)
class MotionEstimate:
    available: bool
    confidence: str
    points_used: int
    current_location: LatLon
    east_velocity_metres_per_second: float | None
    north_velocity_metres_per_second: float | None
    speed_kmh: float | None
    bearing_degrees: float | None
    residual_km: float | None
    smoothed_points: tuple[SmoothedTrackPoint, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "confidence": self.confidence,
            "points_used": self.points_used,
            "current_location": asdict(self.current_location),
            "east_velocity_metres_per_second": self.east_velocity_metres_per_second,
            "north_velocity_metres_per_second": self.north_velocity_metres_per_second,
            "speed_kmh": self.speed_kmh,
            "bearing_degrees": self.bearing_degrees,
            "residual_km": self.residual_km,
            "smoothed_points": [point.as_dict() for point in self.smoothed_points],
        }


def _to_local_plane(location: LatLon, reference: LatLon) -> tuple[float, float]:
    east = (
        EARTH_RADIUS_M
        * cos(radians(reference.latitude))
        * radians(location.longitude - reference.longitude)
    )
    north = EARTH_RADIUS_M * radians(location.latitude - reference.latitude)
    return east, north


def _from_local_plane(east: float, north: float, reference: LatLon) -> LatLon:
    return LatLon(
        latitude=reference.latitude + degrees(north / EARTH_RADIUS_M),
        longitude=reference.longitude
        + degrees(east / (EARTH_RADIUS_M * cos(radians(reference.latitude)))),
    )


def _weighted_slope(times: list[float], values: list[float], weights: list[float]) -> float:
    total_weight = sum(weights)
    mean_time = sum(weight * value for weight, value in zip(weights, times)) / total_weight
    mean_value = sum(weight * value for weight, value in zip(weights, values)) / total_weight
    denominator = sum(weight * (time - mean_time) ** 2 for weight, time in zip(weights, times))
    if denominator == 0:
        return 0.0
    numerator = sum(
        weight * (time - mean_time) * (value - mean_value)
        for weight, time, value in zip(weights, times, values)
    )
    return numerator / denominator


def estimate_track_motion(track: "StormTrack", window_observations: int = 5) -> MotionEstimate:
    """Fit a short, recency-weighted velocity without discarding raw points.

    The fitted line is anchored to the latest observed centroid. This smooths
    historic centroid jitter while ensuring a live projection always starts at
    the most recently observed storm location.
    """

    if window_observations < 2:
        raise ValueError("window_observations must be at least two")
    points = track.points[-window_observations:]
    latest = points[-1]
    current_location = latest.centroid_location
    if len(points) == 1:
        return MotionEstimate(
            available=False,
            confidence="none",
            points_used=1,
            current_location=current_location,
            east_velocity_metres_per_second=None,
            north_velocity_metres_per_second=None,
            speed_kmh=None,
            bearing_degrees=None,
            residual_km=None,
            smoothed_points=(
                SmoothedTrackPoint(latest.observed_at, latest.centroid_location),
            ),
        )

    times = [
        (point.observed_at - latest.observed_at).total_seconds()
        for point in points
    ]
    coordinates = [_to_local_plane(point.centroid_location, current_location) for point in points]
    # Newer observations count more, but no individual scan can dominate the
    # fit. With two observations this still reproduces the direct vector.
    weights = [float(index + 1) for index in range(len(points))]
    east_velocity = _weighted_slope(times, [value[0] for value in coordinates], weights)
    north_velocity = _weighted_slope(times, [value[1] for value in coordinates], weights)
    speed_metres_per_second = sqrt(east_velocity**2 + north_velocity**2)
    smoothed_points = tuple(
        SmoothedTrackPoint(
            point.observed_at,
            _from_local_plane(
                east_velocity * time,
                north_velocity * time,
                current_location,
            ),
        )
        for point, time in zip(points, times)
    )
    residual_metres = [
        sqrt(
            (observed_east - east_velocity * time) ** 2
            + (observed_north - north_velocity * time) ** 2
        )
        for (observed_east, observed_north), time in zip(coordinates, times)
    ]
    residual_km = sum(residual_metres) / len(residual_metres) / 1_000
    if speed_metres_per_second < 0.01:
        confidence = "none"
    elif len(points) == 2:
        confidence = "medium"
    elif len(points) < window_observations:
        confidence = "medium" if residual_km <= 5 else "low"
    else:
        confidence = "high" if residual_km <= 5 else "medium"
    return MotionEstimate(
        available=speed_metres_per_second >= 0.01,
        confidence=confidence,
        points_used=len(points),
        current_location=current_location,
        east_velocity_metres_per_second=east_velocity,
        north_velocity_metres_per_second=north_velocity,
        speed_kmh=speed_metres_per_second * 3.6,
        bearing_degrees=(degrees(atan2(east_velocity, north_velocity)) + 360) % 360
        if speed_metres_per_second >= 0.01
        else None,
        residual_km=residual_km,
        smoothed_points=smoothed_points,
    )
