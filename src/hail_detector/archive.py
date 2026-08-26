from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .models import RadarFrame


class ArchiveIntegrityError(RuntimeError):
    """A source filename changed after it had already been archived."""


@dataclass(frozen=True)
class ArchivedFrame:
    path: Path
    sha256: str
    already_present: bool


class ArchiveStore:
    """Frame archive with atomic metadata/status writes and bounded history."""

    def __init__(self, root: Path):
        self.root = root
        self.frames_dir = root / "frames"
        self.manifests_dir = root / "manifests"
        self.metadata_dir = root / "metadata"
        self.calibration_dir = root / "calibration"
        self.cells_dir = root / "cells"
        self.tracks_dir = root / "tracks"
        self.assessments_dir = root / "assessments"
        self.alerts_dir = root / "alerts"
        self.notification_outbox_dir = root / "notification-outbox"
        self.alert_audit_dir = root / "alert-audit"
        self.status_dir = root / "status"
        self.opera_dir = root / "opera"
        self.opera_frames_dir = self.opera_dir / "frames"
        self.opera_manifests_dir = self.opera_dir / "manifests"
        self.opera_metadata_dir = self.opera_dir / "metadata"
        self.opera_overlays_dir = self.opera_dir / "overlays"
        self.opera_status_dir = self.opera_dir / "status"
        self.opera_cells_dir = self.opera_dir / "cells"
        self.opera_tracks_dir = self.opera_dir / "tracks"
        self.opera_volume_dir = self.opera_dir / "volume"
        self.opera_volume_frames_dir = self.opera_volume_dir / "frames"
        self.opera_volume_metadata_dir = self.opera_volume_dir / "metadata"
        self.opera_volume_status_dir = self.opera_volume_dir / "status"

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, dir=path.parent) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)

    @staticmethod
    def _json_bytes(data: dict[str, Any]) -> bytes:
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def raw_path(self, frame: RadarFrame) -> Path:
        return self.frames_dir / frame.filename

    def has_raw_frame(self, frame: RadarFrame) -> bool:
        return self.raw_path(frame).is_file()

    def archive_manifest(self, payload: dict[str, Any], fetched_at: datetime) -> Path:
        timestamp = fetched_at.strftime("%Y%m%dT%H%M%S%fZ")
        path = self.manifests_dir / f"JAR-{timestamp}.json"
        self._atomic_write(path, self._json_bytes(payload))
        return path

    def _archive_frame(
        self,
        frame: RadarFrame,
        content: bytes,
        fetched_at: datetime,
        *,
        raw_dir: Path,
        metadata_dir: Path,
        source: str,
    ) -> ArchivedFrame:
        path = raw_dir / frame.filename
        digest = hashlib.sha256(content).hexdigest()
        already_present = path.exists()
        if already_present:
            existing_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing_digest != digest:
                raise ArchiveIntegrityError(
                    f"source filename {frame.filename} changed after archival"
                )
        else:
            self._atomic_write(path, content)

        metadata = {
            "source": source,
            "filename": frame.filename,
            "source_url": frame.url,
            "source_timestamp": frame.source_timestamp,
            "display_timestamp": frame.display_timestamp,
            "fetched_at": fetched_at.isoformat(),
            "sha256": digest,
            "content_bytes": len(content),
        }
        self._atomic_write(
            metadata_dir / f"{frame.filename}.json", self._json_bytes(metadata)
        )
        return ArchivedFrame(path=path, sha256=digest, already_present=already_present)

    @staticmethod
    def _frame_observed_at(raw_path: Path, metadata_path: Path) -> datetime:
        """Read the source observation time, falling back to the file timestamp."""

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            source_timestamp = metadata["source_timestamp"]
            if not isinstance(source_timestamp, str):
                raise ValueError("source_timestamp must be a string")
            observed_at = datetime.fromisoformat(source_timestamp.replace("Z", "+00:00"))
            return (
                observed_at
                if observed_at.tzinfo is not None
                else observed_at.replace(tzinfo=timezone.utc)
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return datetime.fromtimestamp(raw_path.stat().st_mtime, tz=timezone.utc)

    def _prune_source_frames_older_than(
        self,
        cutoff: datetime,
        *,
        raw_dir: Path,
        metadata_dir: Path,
        frame_artifacts: tuple[tuple[Path, str], ...] = (),
    ) -> None:
        if not raw_dir.is_dir():
            return

        for raw_path in raw_dir.iterdir():
            if not raw_path.is_file():
                continue
            metadata_path = metadata_dir / f"{raw_path.name}.json"
            try:
                observed_at = self._frame_observed_at(raw_path, metadata_path)
            except FileNotFoundError:
                # A second poller may have removed this frame between listing
                # the directory and inspecting it. The next pass will handle
                # any remaining files.
                continue
            if observed_at >= cutoff:
                continue

            raw_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            for directory, suffix in frame_artifacts:
                (directory / f"{raw_path.name}{suffix}").unlink(missing_ok=True)

    def prune_frames_older_than(self, cutoff: datetime) -> None:
        """Remove expired raw frames and their per-frame outputs.

        Persistent status and track files deliberately remain: their historic
        points continue to be useful even after their raw source frame expires.
        """

        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")

        self._prune_source_frames_older_than(
            cutoff,
            raw_dir=self.frames_dir,
            metadata_dir=self.metadata_dir,
            frame_artifacts=((self.calibration_dir, ".json"), (self.cells_dir, ".json")),
        )
        self._prune_source_frames_older_than(
            cutoff,
            raw_dir=self.opera_frames_dir,
            metadata_dir=self.opera_metadata_dir,
            frame_artifacts=(
                (self.opera_overlays_dir, ".png"),
                (self.opera_overlays_dir, ".json"),
                (self.opera_cells_dir, ".json"),
            ),
        )
        self._prune_source_frames_older_than(
            cutoff,
            raw_dir=self.opera_volume_frames_dir,
            metadata_dir=self.opera_volume_metadata_dir,
        )

    def archive_frame(self, frame: RadarFrame, content: bytes, fetched_at: datetime) -> ArchivedFrame:
        return self._archive_frame(
            frame,
            content,
            fetched_at,
            raw_dir=self.frames_dir,
            metadata_dir=self.metadata_dir,
            source="yarlovo_cappi",
        )

    def opera_raw_path(self, frame: RadarFrame) -> Path:
        return self.opera_frames_dir / frame.filename

    def has_opera_raw_frame(self, frame: RadarFrame) -> bool:
        return self.opera_raw_path(frame).is_file()

    def archive_opera_manifest(self, payload: dict[str, Any], fetched_at: datetime) -> Path:
        timestamp = fetched_at.strftime("%Y%m%dT%H%M%S%fZ")
        path = self.opera_manifests_dir / f"OPERA-{timestamp}.json"
        self._atomic_write(path, self._json_bytes(payload))
        return path

    def archive_opera_frame(
        self, frame: RadarFrame, content: bytes, fetched_at: datetime
    ) -> ArchivedFrame:
        return self._archive_frame(
            frame,
            content,
            fetched_at,
            raw_dir=self.opera_frames_dir,
            metadata_dir=self.opera_metadata_dir,
            source="eumetnet_opera_dbzh_maximum_reflectivity",
        )

    def opera_volume_raw_path(self, frame: RadarFrame) -> Path:
        return self.opera_volume_frames_dir / frame.filename

    def has_opera_volume_raw_frame(self, frame: RadarFrame) -> bool:
        return self.opera_volume_raw_path(frame).is_file()

    def archive_opera_volume_frame(
        self, frame: RadarFrame, content: bytes, fetched_at: datetime
    ) -> ArchivedFrame:
        return self._archive_frame(
            frame,
            content,
            fetched_at,
            raw_dir=self.opera_volume_frames_dir,
            metadata_dir=self.opera_volume_metadata_dir,
            source="eumetnet_opera_experimental_volume_dbzh",
        )

    def opera_overlay_path(self, frame: RadarFrame) -> Path:
        return self.opera_overlays_dir / f"{frame.filename}.png"

    def has_opera_overlay(self, frame: RadarFrame) -> bool:
        return self.opera_overlay_path(frame).is_file()

    def archive_opera_overlay(
        self, frame: RadarFrame, content: bytes, summary: dict[str, Any]
    ) -> Path:
        path = self.opera_overlay_path(frame)
        if path.exists() and hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(content).digest():
            raise ArchiveIntegrityError(
                f"rendered OPERA overlay {path.name} changed after archival"
            )
        if not path.exists():
            self._atomic_write(path, content)
        self._atomic_write(
            self.opera_overlays_dir / f"{frame.filename}.json", self._json_bytes(summary)
        )
        return path

    def write_opera_cells(
        self,
        frame: RadarFrame,
        cells: list[dict[str, Any]],
        rejected_cells: list[dict[str, Any]] | None = None,
        centroid_weighting: dict[str, Any] | None = None,
    ) -> Path:
        path = self.opera_cells_dir / f"{frame.filename}.json"
        self._atomic_write(
            path,
            self._json_bytes(
                {
                    "source": "eumetnet_opera",
                    "filename": frame.filename,
                    "observed_at": frame.source_timestamp,
                    "cells": cells,
                    "rejected_cells": rejected_cells or [],
                    "centroid_weighting": centroid_weighting,
                }
            ),
        )
        return path

    def write_opera_tracker_state(self, state: dict[str, Any]) -> Path:
        path = self.opera_tracks_dir / "state.json"
        self._atomic_write(path, self._json_bytes(state))
        return path

    def read_opera_tracker_state(self) -> dict[str, Any] | None:
        path = self.opera_tracks_dir / "state.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_opera_history(self, *, since: datetime) -> list[dict[str, str]]:
        """List locally archived OPERA overlays in chronological order."""

        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        history: list[dict[str, str]] = []
        for path in self.opera_metadata_dir.glob("*.json"):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                filename = metadata["filename"]
                observed_at = datetime.fromisoformat(metadata["source_timestamp"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            if observed_at < since or not (self.opera_overlays_dir / f"{filename}.png").is_file():
                continue
            history.append(
                {
                    "filename": filename,
                    "observed_at": observed_at.isoformat(),
                    "display_timestamp": metadata.get("display_timestamp", observed_at.isoformat()),
                }
            )
        return sorted(history, key=lambda frame: (frame["observed_at"], frame["filename"]))

    def write_calibration(self, frame: RadarFrame, summary: dict[str, Any]) -> Path:
        path = self.calibration_dir / f"{frame.filename}.json"
        self._atomic_write(path, self._json_bytes(summary))
        return path

    def write_cells(self, frame: RadarFrame, cells: list[dict[str, Any]]) -> Path:
        path = self.cells_dir / f"{frame.filename}.json"
        self._atomic_write(
            path,
            self._json_bytes(
                {
                    "filename": frame.filename,
                    "observed_at": frame.source_timestamp,
                    "cells": cells,
                }
            ),
        )
        return path

    def write_tracker_state(self, state: dict[str, Any]) -> Path:
        path = self.tracks_dir / "state.json"
        self._atomic_write(path, self._json_bytes(state))
        return path

    def read_tracker_state(self) -> dict[str, Any] | None:
        path = self.tracks_dir / "state.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_assessments(self, assessments: list[dict[str, Any]], target: dict[str, Any]) -> Path:
        path = self.assessments_dir / "latest.json"
        self._atomic_write(path, self._json_bytes({"target": target, "assessments": assessments}))
        return path

    def read_assessments(self) -> dict[str, Any] | None:
        path = self.assessments_dir / "latest.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_alert_state(self, state: dict[str, Any]) -> Path:
        path = self.alerts_dir / "state.json"
        self._atomic_write(path, self._json_bytes(state))
        return path

    def read_alert_state(self) -> dict[str, Any] | None:
        path = self.alerts_dir / "state.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_notification_outbox(self, event: dict[str, Any]) -> Path:
        path = self.notification_outbox_dir / f"{event['event_id']}.json"
        self._atomic_write(path, self._json_bytes(event))
        return path

    def write_alert_audit(self, evaluated_at: datetime, audit: dict[str, Any]) -> Path:
        stamp = evaluated_at.strftime("%Y%m%dT%H%M%S%fZ")
        path = self.alert_audit_dir / f"{stamp}.json"
        self._atomic_write(path, self._json_bytes(audit))
        return path

    def write_status(self, status: dict[str, Any]) -> None:
        self._atomic_write(self.status_dir / "latest.json", self._json_bytes(status))

    def read_status(self) -> dict[str, Any] | None:
        path = self.status_dir / "latest.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_opera_status(self, status: dict[str, Any]) -> None:
        self._atomic_write(self.opera_status_dir / "latest.json", self._json_bytes(status))

    def read_opera_status(self) -> dict[str, Any] | None:
        path = self.opera_status_dir / "latest.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_opera_volume_status(self, status: dict[str, Any]) -> None:
        self._atomic_write(
            self.opera_volume_status_dir / "latest.json", self._json_bytes(status)
        )

    def read_opera_volume_status(self) -> dict[str, Any] | None:
        path = self.opera_volume_status_dir / "latest.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
