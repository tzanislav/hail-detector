from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from math import atan2, cos, degrees, radians, sqrt

from .config import TargetLocation
from .geospatial import LatLon
from .motion import estimate_track_motion
from .tracking import StormTrack


EARTH_RADIUS_M = 6_371_008.8
DEFAULT_PREDICTION_LEAD_MINUTES = (30, 60, 120)
MINIMUM_TRAJECTORY_OBSERVATIONS = 3


@dataclass(frozen=True)
class PredictedPosition:
    lead_minutes: int
    predicted_at: datetime
    location: LatLon

    def as_dict(self) -> dict[str, object]:
        return {
            "lead_minutes": self.lead_minutes,
            "predicted_at": self.predicted_at.isoformat(),
            "location": asdict(self.location),
        }


@dataclass(frozen=True)
class TrajectoryForecast:
    track_id: str
    available: bool
    confidence: str
    reason: str | None
    current_distance_km: float
    speed_kmh: float | None
    bearing_degrees: float | None
    intersects_target_radius: bool
    closest_approach_distance_km: float | None
    closest_approach_at: datetime | None
    arrival_window_start: datetime | None
    arrival_window_end: datetime | None
    predicted_positions: tuple[PredictedPosition, ...] = ()

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        for name in (
            "closest_approach_at",
            "arrival_window_start",
            "arrival_window_end",
        ):
            if data[name] is not None:
                data[name] = data[name].isoformat()
        data["predicted_positions"] = [
            position.as_dict() for position in self.predicted_positions
        ]
        return data


def _to_target_plane(location: LatLon, target: TargetLocation) -> tuple[float, float]:
    """Local east/north coordinates in metres relative to the configured target."""

    east = (
        EARTH_RADIUS_M
        * cos(radians(target.latitude))
        * radians(location.longitude - target.longitude)
    )
    north = EARTH_RADIUS_M * radians(location.latitude - target.latitude)
    return east, north


def _from_target_plane(position: tuple[float, float], target: TargetLocation) -> LatLon:
    return LatLon(
        latitude=target.latitude + degrees(position[1] / EARTH_RADIUS_M),
        longitude=target.longitude
        + degrees(
            position[0] / (EARTH_RADIUS_M * cos(radians(target.latitude)))
        ),
    )


def _distance_km(position: tuple[float, float]) -> float:
    return sqrt(position[0] ** 2 + position[1] ** 2) / 1_000


def _motion_confidence(track: StormTrack, target: TargetLocation) -> str:
    if len(track.points) < 2:
        return "none"
    if len(track.points) == 2:
        return "low"

    latest = track.points[-1]
    middle = track.points[-2]
    earliest = track.points[-3]
    previous_interval = (middle.observed_at - earliest.observed_at).total_seconds()
    latest_interval = (latest.observed_at - middle.observed_at).total_seconds()
    if previous_interval <= 0 or latest_interval <= 0:
        return "low"

    earliest_position = _to_target_plane(earliest.centroid_location, target)
    middle_position = _to_target_plane(middle.centroid_location, target)
    latest_position = _to_target_plane(latest.centroid_location, target)
    previous_velocity = (
        (middle_position[0] - earliest_position[0]) / previous_interval,
        (middle_position[1] - earliest_position[1]) / previous_interval,
    )
    latest_velocity = (
        (latest_position[0] - middle_position[0]) / latest_interval,
        (latest_position[1] - middle_position[1]) / latest_interval,
    )
    previous_speed = sqrt(previous_velocity[0] ** 2 + previous_velocity[1] ** 2)
    latest_speed = sqrt(latest_velocity[0] ** 2 + latest_velocity[1] ** 2)
    if previous_speed == 0 or latest_speed == 0:
        return "low"
    direction_cosine = (
        previous_velocity[0] * latest_velocity[0]
        + previous_velocity[1] * latest_velocity[1]
    ) / (previous_speed * latest_speed)
    speed_ratio = min(previous_speed, latest_speed) / max(previous_speed, latest_speed)
    return "high" if direction_cosine >= 0.866 and speed_ratio >= 0.6 else "medium"


def project_track(
    track: StormTrack,
    target: TargetLocation,
    horizon: timedelta,
    *,
    motion_window_observations: int = 5,
    prediction_lead_minutes: tuple[int, ...] = DEFAULT_PREDICTION_LEAD_MINUTES,
) -> TrajectoryForecast:
    """Project the latest observed motion through a target-radius intersection.

    This is a constant-velocity extrapolation intended for a short, explicit
    horizon. It is a radar-track projection, not a meteorological forecast.
    """

    latest = track.latest
    current_position = _to_target_plane(latest.centroid_location, target)
    current_distance_km = _distance_km(current_position)
    motion = estimate_track_motion(track, motion_window_observations)
    confidence = "low" if len(track.points) == 2 else motion.confidence
    if len(track.points) < MINIMUM_TRAJECTORY_OBSERVATIONS:
        return TrajectoryForecast(
            track_id=track.track_id,
            available=False,
            confidence=confidence,
            reason=(
                f"at least {MINIMUM_TRAJECTORY_OBSERVATIONS} observations are required "
                "before trajectory prediction"
            ),
            current_distance_km=current_distance_km,
            speed_kmh=None,
            bearing_degrees=None,
            intersects_target_radius=False,
            closest_approach_distance_km=None,
            closest_approach_at=None,
            arrival_window_start=None,
            arrival_window_end=None,
        )

    if (
        motion.east_velocity_metres_per_second is None
        or motion.north_velocity_metres_per_second is None
    ):
        return TrajectoryForecast(
            track_id=track.track_id,
            available=False,
            confidence="none",
            reason="motion estimate is unavailable",
            current_distance_km=current_distance_km,
            speed_kmh=None,
            bearing_degrees=None,
            intersects_target_radius=False,
            closest_approach_distance_km=None,
            closest_approach_at=None,
            arrival_window_start=None,
            arrival_window_end=None,
        )
    velocity = (
        motion.east_velocity_metres_per_second,
        motion.north_velocity_metres_per_second,
    )
    velocity_squared = velocity[0] ** 2 + velocity[1] ** 2
    if velocity_squared < 0.01:
        return TrajectoryForecast(
            track_id=track.track_id,
            available=False,
            confidence="none",
            reason="movement is below the minimum resolvable speed",
            current_distance_km=current_distance_km,
            speed_kmh=0.0,
            bearing_degrees=None,
            intersects_target_radius=False,
            closest_approach_distance_km=current_distance_km,
            closest_approach_at=latest.observed_at,
            arrival_window_start=None,
            arrival_window_end=None,
        )

    horizon_seconds = horizon.total_seconds()
    closest_seconds = max(
        0.0,
        min(
            horizon_seconds,
            -(
                current_position[0] * velocity[0]
                + current_position[1] * velocity[1]
            )
            / velocity_squared,
        ),
    )
    closest_position = (
        current_position[0] + velocity[0] * closest_seconds,
        current_position[1] + velocity[1] * closest_seconds,
    )

    radius_metres = target.warning_radius_km * 1_000
    quadratic_b = 2 * (current_position[0] * velocity[0] + current_position[1] * velocity[1])
    quadratic_c = current_position[0] ** 2 + current_position[1] ** 2 - radius_metres**2
    discriminant = quadratic_b**2 - 4 * velocity_squared * quadratic_c
    arrival_start: datetime | None = None
    arrival_end: datetime | None = None
    intersects = False
    if discriminant >= 0:
        root = sqrt(discriminant)
        enter_seconds = (-quadratic_b - root) / (2 * velocity_squared)
        leave_seconds = (-quadratic_b + root) / (2 * velocity_squared)
        window_start = max(0.0, enter_seconds)
        window_end = min(horizon_seconds, leave_seconds)
        if window_start <= window_end and leave_seconds >= 0 and enter_seconds <= horizon_seconds:
            intersects = True
            arrival_start = latest.observed_at + timedelta(seconds=window_start)
            arrival_end = latest.observed_at + timedelta(seconds=window_end)

    speed_kmh = motion.speed_kmh
    bearing_degrees = motion.bearing_degrees
    predictions = tuple(
        PredictedPosition(
            lead_minutes=lead_minutes,
            predicted_at=latest.observed_at + timedelta(minutes=lead_minutes),
            location=_from_target_plane(
                (
                    current_position[0] + velocity[0] * lead_minutes * 60,
                    current_position[1] + velocity[1] * lead_minutes * 60,
                ),
                target,
            ),
        )
        for lead_minutes in sorted(set(prediction_lead_minutes))
        if lead_minutes > 0 and lead_minutes * 60 <= horizon_seconds
    )
    return TrajectoryForecast(
        track_id=track.track_id,
        available=True,
        confidence=confidence,
        reason=None,
        current_distance_km=current_distance_km,
        speed_kmh=speed_kmh,
        bearing_degrees=bearing_degrees,
        intersects_target_radius=intersects,
        closest_approach_distance_km=_distance_km(closest_position),
        closest_approach_at=latest.observed_at + timedelta(seconds=closest_seconds),
        arrival_window_start=arrival_start,
        arrival_window_end=arrival_end,
        predicted_positions=predictions,
    )
