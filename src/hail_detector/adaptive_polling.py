"""Adaptive, rate-conscious cadence for automatic OPERA updates."""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from typing import Any

from .archive import ArchiveStore
from .config import Settings


RAPID_POLL_INTERVAL_SECONDS = 10 * 60
NEARBY_TRACK_DISTANCE_KM = 50.0
RAPID_POLL_HOLD_UPDATES = 5


@dataclass(frozen=True)
class PollCadence:
    """The interval to use after one automatic primary-source update."""

    interval_seconds: int
    rapid_updates_remaining: int
    nearby_track_detected: bool
    standard_interval_seconds: int
    rapid_interval_seconds: int
    nearby_track_distance_km: float

    @property
    def mode(self) -> str:
        return "rapid" if self.rapid_updates_remaining > 0 else "standard"

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "next_poll_interval_seconds": self.interval_seconds,
            "rapid_updates_remaining": self.rapid_updates_remaining,
            "nearby_track_detected": self.nearby_track_detected,
            "nearby_track_distance_km": self.nearby_track_distance_km,
            "rapid_poll_interval_seconds": self.rapid_interval_seconds,
            "standard_poll_interval_seconds": self.standard_interval_seconds,
        }


def _number(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if result == result and abs(result) != float("inf") else None


def _haversine_km(
    first_latitude: float,
    first_longitude: float,
    second_latitude: float,
    second_longitude: float,
) -> float:
    latitude_delta = radians(second_latitude - first_latitude)
    longitude_delta = radians(second_longitude - first_longitude)
    first_latitude_radians = radians(first_latitude)
    second_latitude_radians = radians(second_latitude)
    a = sin(latitude_delta / 2) ** 2 + cos(first_latitude_radians) * cos(
        second_latitude_radians
    ) * sin(longitude_delta / 2) ** 2
    return 2 * 6_371.0088 * asin(sqrt(a))


def _latest_location(track: dict[str, Any]) -> dict[str, Any] | None:
    latest = track.get("latest_centroid")
    if isinstance(latest, dict):
        return latest
    points = track.get("points")
    if not isinstance(points, list) or not points:
        return None
    last_point = points[-1]
    if not isinstance(last_point, dict):
        return None
    location = last_point.get("centroid_location")
    return location if isinstance(location, dict) else None


def has_track_near_protected_area(
    tracker_state: dict[str, Any] | None,
    settings: Settings,
) -> bool:
    """Whether any current track is within the configured protected-circle threshold.

    The threshold is measured from the edge of the configured warning radius,
    rather than its centre.  A track inside the protected circle therefore has
    a distance of zero and always activates rapid polling.
    """

    if not isinstance(tracker_state, dict):
        return False
    tracks = tracker_state.get("tracks")
    if not isinstance(tracks, list):
        return False

    for track in tracks:
        if not isinstance(track, dict):
            continue
        location = _latest_location(track)
        if location is None:
            continue
        latitude = _number(location.get("latitude"))
        longitude = _number(location.get("longitude"))
        if latitude is None or longitude is None:
            continue
        centre_distance_km = _haversine_km(
            settings.target.latitude,
            settings.target.longitude,
            latitude,
            longitude,
        )
        distance_from_area_km = max(
            0.0, centre_distance_km - settings.target.warning_radius_km
        )
        if distance_from_area_km <= settings.nearby_track_distance_km:
            return True
    return False


class AdaptivePollingCadence:
    """Persist the rapid-poll hold across restarts of the poller process."""

    def __init__(self, settings: Settings, archive: ArchiveStore):
        self.settings = settings
        self.archive = archive

    def _rapid_updates_remaining(self) -> int:
        state = self.archive.read_opera_polling_state() or {}
        value = state.get("rapid_updates_remaining")
        if not isinstance(value, int):
            return 0
        return max(0, min(value, self.settings.nearby_track_hold_updates))

    def record_automatic_primary_poll(self, *, primary_data_is_fresh: bool) -> PollCadence:
        """Record one automatic update and return its next polling interval.

        A fresh nearby detection grants five *subsequent* rapid updates.  A
        failed or stale update cannot renew that hold from an old track state;
        it only consumes an update that was already pending.
        """

        nearby_track_detected = primary_data_is_fresh and has_track_near_protected_area(
            self.archive.read_opera_tracker_state(), self.settings
        )
        if nearby_track_detected:
            rapid_updates_remaining = self.settings.nearby_track_hold_updates
        else:
            rapid_updates_remaining = max(0, self._rapid_updates_remaining() - 1)

        interval_seconds = (
            self.settings.nearby_track_poll_interval_seconds
            if rapid_updates_remaining > 0
            else self.settings.poll_interval_seconds
        )
        cadence = PollCadence(
            interval_seconds=interval_seconds,
            rapid_updates_remaining=rapid_updates_remaining,
            nearby_track_detected=nearby_track_detected,
            standard_interval_seconds=self.settings.poll_interval_seconds,
            rapid_interval_seconds=self.settings.nearby_track_poll_interval_seconds,
            nearby_track_distance_km=self.settings.nearby_track_distance_km,
        )
        self.archive.write_opera_polling_state(cadence.as_dict())
        return cadence
