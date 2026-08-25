from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .archive import ArchiveStore
from .config import Settings
from .hail_policy import assess_track
from .models import RadarFrame
from .storm_cells import segment_cells
from .tracking import TrackManager
from .trajectory import project_track


@dataclass(frozen=True)
class AnalysisResult:
    processed_frames: int
    segmented_cells: int
    active_tracks: int
    forecasted_tracks: int
    intersecting_target_tracks: int
    earliest_intersection_at: str | None
    hail_watch_candidates: int
    hail_warning_candidates: int
    errors: tuple[str, ...]


class StormAnalysisPipeline:
    """Process newly archived chronological frames into cells and persistent tracks."""

    def __init__(self, settings: Settings, archive: ArchiveStore):
        self.settings = settings
        self.archive = archive

    def process(self, frames: Iterable[RadarFrame]) -> AnalysisResult:
        manager = TrackManager.from_dict(
            self.archive.read_tracker_state(),
            maximum_gap=self.settings.tracker_maximum_gap,
            maximum_speed_kmh=self.settings.tracker_maximum_speed_kmh,
            motion_window_observations=self.settings.tracker_motion_window_observations,
        )
        processed_frames = 0
        segmented_cells = 0
        errors: list[str] = []

        for frame in sorted(frames, key=lambda item: item.observed_at):
            if manager.last_processed_at and frame.observed_at <= manager.last_processed_at:
                continue
            raw_path = self.archive.raw_path(frame)
            if not raw_path.is_file():
                errors.append(f"{frame.filename}: raw frame is missing from the archive")
                break
            try:
                cells = segment_cells(
                    raw_path.read_bytes(),
                    frame,
                    minimum_dbz=self.settings.segmentation_minimum_dbz,
                    minimum_pixels=self.settings.segmentation_minimum_pixels,
                )
                self.archive.write_cells(frame, [cell.as_dict() for cell in cells])
                manager.associate_frame(frame.observed_at, cells)
            except Exception as error:
                errors.append(f"{frame.filename}: {error}")
                break
            processed_frames += 1
            segmented_cells += len(cells)

        forecasts = [
            project_track(
                track,
                self.settings.target,
                self.settings.forecast_horizon,
                motion_window_observations=self.settings.tracker_motion_window_observations,
            )
            for track in manager.tracks.values()
        ]
        intersections = [
            forecast
            for forecast in forecasts
            if forecast.intersects_target_radius and forecast.arrival_window_start is not None
        ]
        earliest_intersection = min(
            (forecast.arrival_window_start for forecast in intersections), default=None
        )
        tracker_state = manager.as_dict()
        tracker_state["target"] = self.settings.target.as_dict()
        tracker_state["forecast_horizon_minutes"] = int(
            self.settings.forecast_horizon.total_seconds() / 60
        )
        tracker_state["forecasts"] = [forecast.as_dict() for forecast in forecasts]
        self.archive.write_tracker_state(tracker_state)
        forecasts_by_track_id = {forecast.track_id: forecast for forecast in forecasts}
        assessments = [
            assess_track(
                track,
                forecasts_by_track_id[track.track_id],
                self.settings.hail_policy,
            )
            for track in manager.tracks.values()
        ]
        self.archive.write_assessments(
            [assessment.as_dict() for assessment in assessments],
            self.settings.target.as_dict(),
        )
        return AnalysisResult(
            processed_frames=processed_frames,
            segmented_cells=segmented_cells,
            active_tracks=len(manager.tracks),
            forecasted_tracks=sum(forecast.available for forecast in forecasts),
            intersecting_target_tracks=len(intersections),
            earliest_intersection_at=earliest_intersection.isoformat()
            if earliest_intersection
            else None,
            hail_watch_candidates=sum(
                assessment.candidate_level == "Hail Watch" for assessment in assessments
            ),
            hail_warning_candidates=sum(
                assessment.candidate_level == "Hail Warning" for assessment in assessments
            ),
            errors=tuple(errors),
        )
