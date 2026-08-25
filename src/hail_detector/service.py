from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any

from .analysis import AnalysisResult, StormAnalysisPipeline
from .alerting import AlertLifecycle, AlertReconciliationResult
from .archive import ArchiveStore
from .calibration import summarize_png
from .config import Settings
from .models import RadarFrame
from .yarlovo import ManifestSnapshot, YarlovoClient


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollResult:
    state: str
    fetched_at: datetime
    latest_frame: RadarFrame | None
    latest_age_seconds: float | None
    discovered_frames: int
    archived_frames: int
    existing_frames: int
    frame_errors: tuple[str, ...]
    analyzed_frames: int
    segmented_cells: int
    active_tracks: int
    forecasted_tracks: int
    intersecting_target_tracks: int
    earliest_intersection_at: str | None
    hail_watch_candidates: int
    hail_warning_candidates: int
    active_alerts: int
    alert_transitions: int
    notification_outbox_events: int
    alert_processing_deferred: bool
    analysis_errors: tuple[str, ...]
    manifest_etag: str | None
    manifest_last_modified: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_role": "secondary local CAPPI comparison",
            "state": self.state,
            "fetched_at": self.fetched_at.isoformat(),
            "latest_cappi_frame": (
                {
                    "filename": self.latest_frame.filename,
                    "observed_at": self.latest_frame.source_timestamp,
                    "display_timestamp": self.latest_frame.display_timestamp,
                    "url": self.latest_frame.url,
                }
                if self.latest_frame
                else None
            ),
            "latest_age_seconds": self.latest_age_seconds,
            "discovered_cappi_frames": self.discovered_frames,
            "archived_frames": self.archived_frames,
            "existing_frames": self.existing_frames,
            "frame_errors": list(self.frame_errors),
            "analyzed_frames": self.analyzed_frames,
            "segmented_cells": self.segmented_cells,
            "active_tracks": self.active_tracks,
            "forecasted_tracks": self.forecasted_tracks,
            "intersecting_target_tracks": self.intersecting_target_tracks,
            "earliest_intersection_at": self.earliest_intersection_at,
            "hail_watch_candidates": self.hail_watch_candidates,
            "hail_warning_candidates": self.hail_warning_candidates,
            "active_alerts": self.active_alerts,
            "alert_transitions": self.alert_transitions,
            "notification_outbox_events": self.notification_outbox_events,
            "alert_processing_deferred": self.alert_processing_deferred,
            "analysis_errors": list(self.analysis_errors),
            "manifest_etag": self.manifest_etag,
            "manifest_last_modified": self.manifest_last_modified,
            "error": self.error,
        }


class IngestionService:
    """Fetch, archive, calibrate and safety-check Yarlovo CAPPI frames."""

    def __init__(
        self,
        settings: Settings,
        archive: ArchiveStore,
        client: YarlovoClient,
        analysis: StormAnalysisPipeline | None = None,
        alert_lifecycle: AlertLifecycle | None = None,
    ):
        self.settings = settings
        self.archive = archive
        self.client = client
        self.analysis = analysis or StormAnalysisPipeline(settings, archive)
        self.alert_lifecycle = alert_lifecycle or AlertLifecycle(
            archive, settings.alert_lifecycle.expiry
        )

    def _write_error(self, fetched_at: datetime, error: Exception) -> PollResult:
        result = PollResult(
            state="error",
            fetched_at=fetched_at,
            latest_frame=None,
            latest_age_seconds=None,
            discovered_frames=0,
            archived_frames=0,
            existing_frames=0,
            frame_errors=(),
            analyzed_frames=0,
            segmented_cells=0,
            active_tracks=0,
            forecasted_tracks=0,
            intersecting_target_tracks=0,
            earliest_intersection_at=None,
            hail_watch_candidates=0,
            hail_warning_candidates=0,
            active_alerts=0,
            alert_transitions=0,
            notification_outbox_events=0,
            alert_processing_deferred=True,
            analysis_errors=(),
            manifest_etag=None,
            manifest_last_modified=None,
            error=str(error),
        )
        self.archive.write_status(result.as_dict())
        return result

    def poll_once(self, now: datetime | None = None) -> PollResult:
        fetched_at = now or datetime.now(timezone.utc)
        if fetched_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        try:
            snapshot = self.client.fetch_manifest()
            # The client timestamps after a successful response; use that for
            # source-age decisions and for the manifest archive name.
            fetched_at = snapshot.fetched_at
            self.archive.archive_manifest(snapshot.payload, fetched_at)
        except Exception as error:  # A persisted error is safer than a crashed poller.
            LOGGER.exception("Yarlovo manifest fetch failed")
            return self._write_error(fetched_at, error)

        return self._process_snapshot(snapshot)

    def _process_snapshot(self, snapshot: ManifestSnapshot) -> PollResult:
        frames = snapshot.frames
        latest_frame = frames[-1] if frames else None
        latest_age_seconds = (
            (snapshot.fetched_at - latest_frame.observed_at.astimezone(timezone.utc)).total_seconds()
            if latest_frame
            else None
        )
        is_stale = latest_age_seconds is None or latest_age_seconds > self.settings.maximum_frame_age.total_seconds()

        archived_frames = 0
        existing_frames = 0
        frame_errors: list[str] = []
        for frame in frames:
            try:
                if self.archive.has_raw_frame(frame):
                    existing_frames += 1
                    continue
                content = self.client.download_frame(frame)
                self.archive.archive_frame(frame, content, snapshot.fetched_at)
                self.archive.write_calibration(frame, summarize_png(content).as_dict())
                archived_frames += 1
            except Exception as error:
                LOGGER.exception("Could not archive frame %s", frame.filename)
                frame_errors.append(f"{frame.filename}: {error}")

        latest_is_archived = latest_frame is not None and self.archive.has_raw_frame(latest_frame)
        if self.settings.yarlovo_analysis_enabled:
            analysis_result = self.analysis.process(frames)
            assessments = (self.archive.read_assessments() or {}).get("assessments", [])
            alert_result = self.alert_lifecycle.reconcile(
                assessments,
                evaluated_at=snapshot.fetched_at,
                data_is_fresh=(
                    latest_is_archived
                    and not is_stale
                    and not frame_errors
                    and not analysis_result.errors
                ),
            )
        else:
            analysis_result = AnalysisResult(
                processed_frames=0,
                segmented_cells=0,
                active_tracks=0,
                forecasted_tracks=0,
                intersecting_target_tracks=0,
                earliest_intersection_at=None,
                hail_watch_candidates=0,
                hail_warning_candidates=0,
                errors=(),
            )
            alert_result = AlertReconciliationResult(
                active_alerts=0,
                transitions=(),
                outbox_events=0,
                deferred_for_stale_data=True,
            )
        if not frames:
            state = "error"
            error = "manifest contains no valid CAPPI frames"
        elif not latest_is_archived:
            state = "error"
            error = "latest valid CAPPI frame could not be archived"
        elif analysis_result.errors:
            state = "error"
            error = "storm-cell analysis could not process all chronological frames"
        elif is_stale:
            state = "stale"
            error = "latest valid CAPPI frame exceeds configured maximum age"
        else:
            state = "ready"
            error = None

        result = PollResult(
            state=state,
            fetched_at=snapshot.fetched_at,
            latest_frame=latest_frame,
            latest_age_seconds=latest_age_seconds,
            discovered_frames=len(frames),
            archived_frames=archived_frames,
            existing_frames=existing_frames,
            frame_errors=tuple(frame_errors),
            analyzed_frames=analysis_result.processed_frames,
            segmented_cells=analysis_result.segmented_cells,
            active_tracks=analysis_result.active_tracks,
            forecasted_tracks=analysis_result.forecasted_tracks,
            intersecting_target_tracks=analysis_result.intersecting_target_tracks,
            earliest_intersection_at=analysis_result.earliest_intersection_at,
            hail_watch_candidates=analysis_result.hail_watch_candidates,
            hail_warning_candidates=analysis_result.hail_warning_candidates,
            active_alerts=alert_result.active_alerts,
            alert_transitions=len(alert_result.transitions),
            notification_outbox_events=alert_result.outbox_events,
            alert_processing_deferred=alert_result.deferred_for_stale_data,
            analysis_errors=analysis_result.errors,
            manifest_etag=snapshot.etag,
            manifest_last_modified=snapshot.last_modified,
            error=error,
        )
        self.archive.write_status(result.as_dict())
        return result
