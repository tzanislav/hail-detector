from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from math import asin, atan2, cos, degrees, radians, sin, sqrt
from typing import Any

from .geospatial import LatLon, PixelCoordinate, RadarImageBounds, YARLOVO_BOUNDS
from .motion import estimate_track_motion
from .storm_cells import StormCell


@dataclass(frozen=True)
class TrackPoint:
    observed_at: datetime
    cell_id: str
    centroid_pixel: PixelCoordinate
    centroid_location: LatLon
    pixel_count: int
    area_square_km: float
    area_at_or_above_square_km: dict[str, float]
    max_dbz_lower: int
    max_dbz_upper: int

    @classmethod
    def from_cell(cls, cell: StormCell) -> "TrackPoint":
        return cls(
            observed_at=cell.observed_at,
            cell_id=cell.cell_id,
            centroid_pixel=cell.centroid_pixel,
            centroid_location=cell.centroid_location,
            pixel_count=cell.pixel_count,
            area_square_km=cell.area_square_km,
            area_at_or_above_square_km=cell.area_at_or_above_square_km,
            max_dbz_lower=cell.max_band.lower_dbz,
            max_dbz_upper=cell.max_band.upper_dbz,
        )

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["observed_at"] = self.observed_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TrackPoint":
        return cls(
            observed_at=datetime.fromisoformat(value["observed_at"]),
            cell_id=value["cell_id"],
            centroid_pixel=PixelCoordinate(**value["centroid_pixel"]),
            centroid_location=LatLon(**value["centroid_location"]),
            pixel_count=value["pixel_count"],
            area_square_km=value["area_square_km"],
            area_at_or_above_square_km=value.get("area_at_or_above_square_km", {}),
            max_dbz_lower=value["max_dbz_lower"],
            max_dbz_upper=value["max_dbz_upper"],
        )


@dataclass
class StormTrack:
    track_id: str
    points: list[TrackPoint] = field(default_factory=list)

    @property
    def latest(self) -> TrackPoint:
        return self.points[-1]

    def as_dict(self) -> dict[str, object]:
        return {"track_id": self.track_id, "points": [point.as_dict() for point in self.points]}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StormTrack":
        return cls(
            track_id=value["track_id"],
            points=[TrackPoint.from_dict(point) for point in value["points"]],
        )


@dataclass(frozen=True)
class FrameTrackResult:
    frame_filename: str
    matched_cell_ids: tuple[str, ...]
    new_track_ids: tuple[str, ...]


def _distance_pixels(first: PixelCoordinate, second: PixelCoordinate) -> float:
    return sqrt((first.column - second.column) ** 2 + (first.row - second.row) ** 2)


def _haversine_km(first: LatLon, second: LatLon) -> float:
    radius_km = 6_371.0088
    latitude_delta = radians(second.latitude - first.latitude)
    longitude_delta = radians(second.longitude - first.longitude)
    first_latitude = radians(first.latitude)
    second_latitude = radians(second.latitude)
    a = sin(latitude_delta / 2) ** 2 + cos(first_latitude) * cos(second_latitude) * sin(
        longitude_delta / 2
    ) ** 2
    return 2 * radius_km * asin(sqrt(a))


def _bearing_degrees(first: LatLon, second: LatLon) -> float:
    longitude_delta = radians(second.longitude - first.longitude)
    first_latitude = radians(first.latitude)
    second_latitude = radians(second.latitude)
    x = sin(longitude_delta) * cos(second_latitude)
    y = cos(first_latitude) * sin(second_latitude) - sin(first_latitude) * cos(
        second_latitude
    ) * cos(longitude_delta)
    return (degrees(atan2(x, y)) + 360) % 360


class TrackManager:
    """One-to-one nearest-prediction association for chronologically ordered cells."""

    def __init__(
        self,
        *,
        maximum_gap: timedelta,
        maximum_speed_kmh: int,
        bounds: RadarImageBounds = YARLOVO_BOUNDS,
        motion_window_observations: int = 5,
        tracks: dict[str, StormTrack] | None = None,
        last_processed_at: datetime | None = None,
        next_track_number: int = 1,
    ):
        self.maximum_gap = maximum_gap
        self.maximum_speed_kmh = maximum_speed_kmh
        self.bounds = bounds
        if motion_window_observations < 2:
            raise ValueError("motion_window_observations must be at least two")
        self.motion_window_observations = motion_window_observations
        self.tracks = tracks or {}
        self.last_processed_at = last_processed_at
        self.next_track_number = next_track_number

    def _maximum_speed_pixels_per_second(self, reference: PixelCoordinate) -> float:
        metres_per_second = self.maximum_speed_kmh / 3.6
        # Use the smaller local ground dimension to make the gate conservative
        # for a storm moving along the denser image axis.
        metres_per_pixel = min(
            self.bounds.ground_resolution_at(reference.column, reference.row)
        )
        return metres_per_second / metres_per_pixel

    def _predicted_pixel(self, track: StormTrack, at_time: datetime) -> PixelCoordinate:
        latest = track.latest
        if len(track.points) < 2:
            return latest.centroid_pixel
        previous = track.points[-2]
        observed_delta = (latest.observed_at - previous.observed_at).total_seconds()
        if observed_delta <= 0:
            return latest.centroid_pixel
        future_delta = (at_time - latest.observed_at).total_seconds()
        return PixelCoordinate(
            column=latest.centroid_pixel.column
            + (latest.centroid_pixel.column - previous.centroid_pixel.column)
            / observed_delta
            * future_delta,
            row=latest.centroid_pixel.row
            + (latest.centroid_pixel.row - previous.centroid_pixel.row)
            / observed_delta
            * future_delta,
        )

    def _gate_pixels(self, elapsed_seconds: float, reference: PixelCoordinate) -> float:
        # Twelve pixels covers centre shifts and segmentation jitter; the speed
        # term keeps genuine fast cells eligible without allowing distant jumps.
        return max(
            12.0,
            self._maximum_speed_pixels_per_second(reference) * elapsed_seconds * 1.25,
        )

    def prune_stale_tracks(self, at_time: datetime) -> tuple[str, ...]:
        """Retire tracks that have been absent longer than their association gap.

        A track is useful only while a later observation can still be
        associated with it.  Removing it here prevents a disappeared echo
        from remaining on the operational map indefinitely.  The per-frame
        cell archive remains the audit record for retired tracks.
        """

        if at_time.tzinfo is None:
            raise ValueError("at_time must be timezone-aware")
        stale_track_ids = tuple(
            track_id
            for track_id, track in self.tracks.items()
            if at_time - track.latest.observed_at > self.maximum_gap
        )
        for track_id in stale_track_ids:
            del self.tracks[track_id]
        return stale_track_ids

    def associate_frame(self, observed_at: datetime, cells: list[StormCell]) -> FrameTrackResult:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.last_processed_at and observed_at <= self.last_processed_at:
            raise ValueError("frames must be associated in strictly chronological order")

        self.prune_stale_tracks(observed_at)

        candidates: list[tuple[float, str, int]] = []
        for track in self.tracks.values():
            elapsed_seconds = (observed_at - track.latest.observed_at).total_seconds()
            if elapsed_seconds <= 0 or elapsed_seconds > self.maximum_gap.total_seconds():
                continue
            predicted = self._predicted_pixel(track, observed_at)
            gate = self._gate_pixels(elapsed_seconds, track.latest.centroid_pixel)
            for index, cell in enumerate(cells):
                distance = _distance_pixels(predicted, cell.centroid_pixel)
                if distance <= gate:
                    candidates.append((distance, track.track_id, index))

        matched_tracks: set[str] = set()
        matched_cells: set[int] = set()
        for _, track_id, cell_index in sorted(candidates):
            if track_id in matched_tracks or cell_index in matched_cells:
                continue
            self.tracks[track_id].points.append(TrackPoint.from_cell(cells[cell_index]))
            matched_tracks.add(track_id)
            matched_cells.add(cell_index)

        new_track_ids: list[str] = []
        for index, cell in enumerate(cells):
            if index in matched_cells:
                continue
            track_id = f"track-{self.next_track_number:06d}"
            self.next_track_number += 1
            self.tracks[track_id] = StormTrack(track_id=track_id, points=[TrackPoint.from_cell(cell)])
            new_track_ids.append(track_id)

        self.last_processed_at = observed_at
        return FrameTrackResult(
            frame_filename=cells[0].frame_filename if cells else "",
            matched_cell_ids=tuple(cells[index].cell_id for index in sorted(matched_cells)),
            new_track_ids=tuple(new_track_ids),
        )

    def track_summary(self, track: StormTrack) -> dict[str, object]:
        latest = track.latest
        motion = estimate_track_motion(track, self.motion_window_observations)
        summary: dict[str, object] = {
            "track_id": track.track_id,
            "point_count": len(track.points),
            "latest_cell_id": latest.cell_id,
            "latest_observed_at": latest.observed_at.isoformat(),
            "latest_centroid": asdict(latest.centroid_location),
            "latest_max_dbz_band": [latest.max_dbz_lower, latest.max_dbz_upper],
            "latest_area_square_km": latest.area_square_km,
            "latest_area_at_or_above_square_km": latest.area_at_or_above_square_km,
            "speed_kmh": motion.speed_kmh,
            "bearing_degrees": motion.bearing_degrees,
            "intensity_trend": "unknown",
            "motion_confidence": motion.confidence,
            "motion_filter": motion.as_dict(),
            "smoothed_points": [point.as_dict() for point in motion.smoothed_points],
        }
        if len(track.points) < 2:
            return summary

        previous = track.points[-2]
        intensity_change = latest.max_dbz_lower - previous.max_dbz_lower
        if intensity_change >= 5:
            summary["intensity_trend"] = "strengthening"
        elif intensity_change <= -5:
            summary["intensity_trend"] = "weakening"
        else:
            summary["intensity_trend"] = "steady"
        return summary

    def as_dict(self) -> dict[str, object]:
        return {
            "last_processed_at": self.last_processed_at.isoformat()
            if self.last_processed_at
            else None,
            "next_track_number": self.next_track_number,
            "motion_window_observations": self.motion_window_observations,
            "tracks": [self.track_summary(track) | {"points": [point.as_dict() for point in track.points]} for track in self.tracks.values()],
        }

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any] | None,
        *,
        maximum_gap: timedelta,
        maximum_speed_kmh: int,
        bounds: RadarImageBounds = YARLOVO_BOUNDS,
        motion_window_observations: int = 5,
    ) -> "TrackManager":
        if value is None:
            return cls(
                maximum_gap=maximum_gap,
                maximum_speed_kmh=maximum_speed_kmh,
                bounds=bounds,
                motion_window_observations=motion_window_observations,
            )
        tracks = {
            item["track_id"]: StormTrack.from_dict(item)
            for item in value.get("tracks", [])
        }
        last_processed_at = value.get("last_processed_at")
        return cls(
            maximum_gap=maximum_gap,
            maximum_speed_kmh=maximum_speed_kmh,
            bounds=bounds,
            motion_window_observations=value.get(
                "motion_window_observations", motion_window_observations
            ),
            tracks=tracks,
            last_processed_at=datetime.fromisoformat(last_processed_at)
            if last_processed_at
            else None,
            next_track_number=value.get("next_track_number", 1),
        )
