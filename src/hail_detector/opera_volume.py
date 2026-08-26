from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from math import radians, sin, sqrt
import re
from typing import Any
from urllib.parse import unquote, urlparse

import h5py
import httpx
import numpy as np
from pyproj import Geod

from .archive import ArchiveStore
from .config import Settings
from .models import RadarFrame


VOLUME_FILENAME_PATTERN = re.compile(
    r"^[A-Za-z0-9_-]+@(?P<timestamp>\d{8}T\d{4,6})@[^@]+@DBZH\.h5$",
    re.IGNORECASE,
)
EARTH_RADIUS_METRES = 6_371_000.0
EFFECTIVE_EARTH_RADIUS_METRES = EARTH_RADIUS_METRES * 4.0 / 3.0
GEODESIC = Geod(ellps="WGS84")


class OperaVolumeProtocolError(RuntimeError):
    """Raised when an OPERA volume response or ODIM file is unexpected."""


@dataclass(frozen=True)
class OperaVolumeSnapshot:
    payload: dict[str, Any]
    frames: tuple[RadarFrame, ...]
    fetched_at: datetime


@dataclass(frozen=True)
class OperaVolumePollResult:
    state: str
    fetched_at: datetime
    latest_frame: RadarFrame | None
    latest_age_seconds: float | None
    discovered_frames: int
    archived_frames: int
    existing_frames: int
    track_profiles: tuple[dict[str, Any], ...]
    frame_errors: tuple[str, ...]
    error: str | None = None

    def as_dict(self, settings: Settings) -> dict[str, Any]:
        return {
            "source": "EUMETNET OPERA experimental DBZH volume profile",
            "state": self.state,
            "experimental": True,
            "used_for": [],
            "not_used_for": ["tracking", "trajectory", "hail alerts"],
            "fetched_at": self.fetched_at.isoformat(),
            "radar": {
                "platform_id": settings.opera_volume_radar_id,
                "name": settings.opera_volume_radar_name,
            },
            "latest_dbzh_volume": (
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
            "discovered_dbzh_volumes": self.discovered_frames,
            "archived_frames": self.archived_frames,
            "existing_frames": self.existing_frames,
            "track_profiles": list(self.track_profiles),
            "frame_errors": list(self.frame_errors),
            "limitation": (
                "This distant volume radar is an experimental vertical-reflectivity "
                "observer only; it cannot alter the primary OPERA composite track."
            ),
            "error": self.error,
        }


def _filename_from_url(url: str) -> str:
    return unquote(urlparse(url).path.rsplit("/", maxsplit=1)[-1])


def _decode_attribute(value: object) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _attribute_number(attributes: Any, name: str) -> float:
    try:
        return float(attributes[name])
    except KeyError as error:
        raise OperaVolumeProtocolError(f"OPERA volume metadata has no {name!r} attribute") from error


def parse_opera_volume_frames(payload: dict[str, Any]) -> tuple[RadarFrame, ...]:
    """Extract DBZH PVOL download links from the API's nested CoverageJSON."""

    coverages = payload.get("coverages")
    if not isinstance(coverages, list):
        raise OperaVolumeProtocolError("OPERA volume response has no coverages list")

    frames: list[RadarFrame] = []
    for coverage in coverages:
        if not isinstance(coverage, dict):
            continue
        links = coverage.get("links")
        if not isinstance(links, list):
            continue
        for item in links:
            if not isinstance(item, dict):
                continue
            url = item.get("href")
            if item.get("type") != "application/x-odim" or not isinstance(url, str):
                continue
            filename = _filename_from_url(url)
            match = VOLUME_FILENAME_PATTERN.fullmatch(filename)
            if match is None:
                continue
            timestamp = match.group("timestamp")
            timestamp_format = "%Y%m%dT%H%M%S" if len(timestamp) == 15 else "%Y%m%dT%H%M"
            observed_at = datetime.strptime(timestamp, timestamp_format).replace(tzinfo=timezone.utc)
            frames.append(
                RadarFrame(
                    filename=filename,
                    url=url,
                    observed_at=observed_at,
                    display_timestamp=observed_at.strftime("%d.%m.%Y %H:%M UTC"),
                )
            )
    unique_by_filename = {frame.filename: frame for frame in frames}
    return tuple(sorted(unique_by_filename.values(), key=lambda frame: (frame.observed_at, frame.filename)))


class OperaVolumeClient:
    """Client for one OPERA DBZH polar-volume radar location."""

    def __init__(
        self,
        api_url: str,
        lookback: timedelta,
        client: httpx.Client | None = None,
    ):
        self.api_url = api_url
        self.lookback = lookback
        self._client = client or httpx.Client(timeout=60.0, follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OperaVolumeClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch_snapshot(self, now: datetime | None = None) -> OperaVolumeSnapshot:
        fetched_at = now or datetime.now(timezone.utc)
        if fetched_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        fetched_at = fetched_at.astimezone(timezone.utc)
        start = fetched_at - self.lookback
        response = self._client.get(
            self.api_url,
            params={
                "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{fetched_at:%Y-%m-%dT%H:%M:%SZ}",
                "f": "CoverageJSON",
                "standard_name": "DBZH",
                "format": "ODIM",
                "method": "scan",
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise OperaVolumeProtocolError("OPERA volume response is not valid JSON") from error
        if not isinstance(payload, dict):
            raise OperaVolumeProtocolError("OPERA volume response root must be a JSON object")
        return OperaVolumeSnapshot(
            payload=payload,
            frames=parse_opera_volume_frames(payload),
            fetched_at=fetched_at,
        )

    def download_frame(self, frame: RadarFrame) -> bytes:
        response = self._client.get(frame.url)
        response.raise_for_status()
        if not response.content.startswith(b"\x89HDF\r\n\x1a\n"):
            raise OperaVolumeProtocolError(f"{frame.filename} is not an HDF5 response")
        return response.content


def _nearest_ray_index(dataset: h5py.Group, bearing_degrees: float, nrays: int) -> int:
    start_azimuths = dataset.get("how", {}).attrs.get("startazA") if "how" in dataset else None
    if start_azimuths is not None:
        angles = np.asarray(start_azimuths, dtype=np.float64)
        if angles.size == nrays:
            separation = np.abs((angles - bearing_degrees + 180.0) % 360.0 - 180.0)
            return int(np.argmin(separation))
    a1gate = int(dataset["where"].attrs.get("a1gate", 0))
    return int(round(bearing_degrees / 360.0 * nrays + a1gate)) % nrays


def _beam_height_metres(range_metres: float, elevation_degrees: float, radar_height_metres: float) -> float:
    elevation = radians(elevation_degrees)
    return (
        sqrt(
            range_metres * range_metres
            + EFFECTIVE_EARTH_RADIUS_METRES * EFFECTIVE_EARTH_RADIUS_METRES
            + 2.0 * range_metres * EFFECTIVE_EARTH_RADIUS_METRES * sin(elevation)
        )
        - EFFECTIVE_EARTH_RADIUS_METRES
        + radar_height_metres
    )


def _profile_sample(
    dataset: h5py.Group,
    *,
    range_metres: float,
    bearing_degrees: float,
    radar_height_metres: float,
    maximum_beam_height_metres: int,
) -> dict[str, Any] | None:
    where = dataset["where"].attrs
    elevation = _attribute_number(where, "elangle")
    beam_height = _beam_height_metres(range_metres, elevation, radar_height_metres)
    if beam_height > maximum_beam_height_metres:
        return None
    nrays = int(_attribute_number(where, "nrays"))
    nbins = int(_attribute_number(where, "nbins"))
    rscale = _attribute_number(where, "rscale")
    rstart_metres = _attribute_number(where, "rstart") * 1_000.0
    bin_index = int((range_metres - rstart_metres) / rscale)
    if not 0 <= bin_index < nbins:
        return None
    data_group = next(
        (
            dataset[name]
            for name in dataset
            if name.startswith("data")
            and _decode_attribute(dataset[name]["what"].attrs.get("quantity", "")) == "DBZH"
        ),
        None,
    )
    if data_group is None:
        raise OperaVolumeProtocolError("OPERA volume scan has no DBZH data group")
    raw = data_group["data"]
    ray_index = _nearest_ray_index(dataset, bearing_degrees, nrays)
    ray_indices = np.array([(ray_index - 1) % nrays, ray_index, (ray_index + 1) % nrays])
    bin_start = max(0, bin_index - 1)
    bin_end = min(nbins, bin_index + 2)
    # h5py requires fancy indices to be sorted; the local ray neighbourhood
    # crosses north for a bearing near 0°, so read each wrapped row directly.
    values = np.asarray(
        [raw[int(ray), bin_start:bin_end] for ray in ray_indices], dtype=np.float64
    )
    data_what = data_group["what"].attrs
    nodata = _attribute_number(data_what, "nodata")
    undetect = _attribute_number(data_what, "undetect")
    valid = np.isfinite(values) & ~np.isclose(values, nodata) & ~np.isclose(values, undetect)
    maximum_dbzh: float | None = None
    if np.any(valid):
        gain = _attribute_number(data_what, "gain")
        offset = _attribute_number(data_what, "offset")
        maximum_dbzh = float((values[valid] * gain + offset).max())
    return {
        "elevation_degrees": round(elevation, 2),
        "beam_height_metres_msl": round(beam_height),
        "max_dbzh": round(maximum_dbzh, 1) if maximum_dbzh is not None else None,
    }


def extract_opera_volume_profile(
    content: bytes,
    *,
    latitude: float,
    longitude: float,
    maximum_beam_height_metres: int,
) -> dict[str, Any]:
    """Sample one polar DBZH volume at a location as a vertical profile.

    A three-ray by three-gate local maximum avoids a false empty profile caused
    by a small time/position mismatch between a five-minute composite and its
    source volume. It is reported as a local maximum, never an exact point dBZ.
    """

    try:
        with h5py.File(BytesIO(content), "r") as source:
            root_what = source["what"].attrs
            if _decode_attribute(root_what.get("object", "")) != "PVOL":
                raise OperaVolumeProtocolError("expected an ODIM PVOL volume")
            root_where = source["where"].attrs
            radar_latitude = _attribute_number(root_where, "lat")
            radar_longitude = _attribute_number(root_where, "lon")
            radar_height = _attribute_number(root_where, "height")
            bearing, _, distance_metres = GEODESIC.inv(
                radar_longitude, radar_latitude, longitude, latitude
            )
            bearing_degrees = bearing % 360.0
            samples: list[dict[str, Any]] = []
            scan_count = 0
            for name in sorted((name for name in source if name.startswith("dataset"))):
                dataset = source[name]
                if _decode_attribute(dataset["what"].attrs.get("product", "")) != "SCAN":
                    continue
                scan_count += 1
                sample = _profile_sample(
                    dataset,
                    range_metres=distance_metres,
                    bearing_degrees=bearing_degrees,
                    radar_height_metres=radar_height,
                    maximum_beam_height_metres=maximum_beam_height_metres,
                )
                if sample is not None:
                    samples.append(sample)
    except (KeyError, OSError, ValueError) as error:
        raise OperaVolumeProtocolError(f"could not read OPERA volume: {error}") from error

    return {
        "radar_location": {"latitude": radar_latitude, "longitude": radar_longitude},
        "sample_location": {"latitude": latitude, "longitude": longitude},
        "distance_km": round(distance_metres / 1_000.0, 1),
        "bearing_degrees": round(bearing_degrees, 1),
        "scan_count": scan_count,
        "profile": sorted(samples, key=lambda sample: sample["elevation_degrees"]),
    }


def _confirmed_track_locations(archive: ArchiveStore, minimum_confirmations: int) -> list[tuple[str, dict[str, Any]]]:
    state = archive.read_opera_tracker_state() or {}
    confirmed: list[tuple[str, dict[str, Any]]] = []
    for track in state.get("tracks", []):
        points = track.get("points", []) if isinstance(track, dict) else []
        if len(points) < minimum_confirmations or not points:
            continue
        latest_point = points[-1]
        location = latest_point.get("centroid_location", {}) if isinstance(latest_point, dict) else {}
        latitude, longitude = location.get("latitude"), location.get("longitude")
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            continue
        track_id = track.get("track_id")
        if not isinstance(track_id, str):
            continue
        confirmed.append((track_id, {"latitude": float(latitude), "longitude": float(longitude)}))
    return confirmed


class OperaVolumeIngestionService:
    """Archive and sample the experimental distant OPERA polar volume.

    This service is intentionally independent of the primary composite
    ingestion. A missing or stale volume is visible in status but can never
    downgrade health, suppress a primary track, or influence an alert.
    """

    def __init__(self, settings: Settings, archive: ArchiveStore, client: OperaVolumeClient):
        self.settings = settings
        self.archive = archive
        self.client = client

    def _write_error(self, fetched_at: datetime, error: Exception) -> OperaVolumePollResult:
        self.archive.prune_frames_older_than(fetched_at - self.settings.frame_retention)
        result = OperaVolumePollResult(
            state="error",
            fetched_at=fetched_at,
            latest_frame=None,
            latest_age_seconds=None,
            discovered_frames=0,
            archived_frames=0,
            existing_frames=0,
            track_profiles=(),
            frame_errors=(),
            error=str(error),
        )
        self.archive.write_opera_volume_status(result.as_dict(self.settings))
        return result

    def poll_once(self, now: datetime | None = None) -> OperaVolumePollResult:
        fetched_at = now or datetime.now(timezone.utc)
        if fetched_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        try:
            snapshot = self.client.fetch_snapshot(fetched_at)
            fetched_at = snapshot.fetched_at
        except Exception as error:
            return self._write_error(fetched_at, error)

        frames = snapshot.frames
        latest_frame = frames[-1] if frames else None
        latest_age_seconds = (
            (fetched_at - latest_frame.observed_at).total_seconds() if latest_frame else None
        )
        archived_frames = 0
        existing_frames = 0
        frame_errors: list[str] = []
        for frame in frames:
            try:
                if self.archive.has_opera_volume_raw_frame(frame):
                    existing_frames += 1
                    continue
                content = self.client.download_frame(frame)
                self.archive.archive_opera_volume_frame(frame, content, fetched_at)
                archived_frames += 1
            except Exception as error:
                frame_errors.append(f"{frame.filename}: {error}")

        track_profiles: list[dict[str, Any]] = []
        if latest_frame is not None and self.archive.has_opera_volume_raw_frame(latest_frame):
            try:
                content = self.archive.opera_volume_raw_path(latest_frame).read_bytes()
                for track_id, location in _confirmed_track_locations(
                    self.archive, self.settings.opera_track_minimum_confirmations
                ):
                    profile = extract_opera_volume_profile(
                        content,
                        latitude=location["latitude"],
                        longitude=location["longitude"],
                        maximum_beam_height_metres=self.settings.opera_volume_maximum_beam_height_metres,
                    )
                    track_profiles.append({"track_id": track_id, **profile})
            except Exception as error:
                frame_errors.append(f"{latest_frame.filename}: profile extraction failed: {error}")

        self.archive.prune_frames_older_than(fetched_at - self.settings.frame_retention)
        latest_is_archived = latest_frame is not None and self.archive.has_opera_volume_raw_frame(latest_frame)
        if not frames:
            state, error = "error", "OPERA response contains no DBZH volume links"
        elif not latest_is_archived:
            state, error = "error", "latest OPERA volume could not be archived"
        elif frame_errors:
            state, error = "degraded", "one or more OPERA volumes could not be archived or sampled"
        elif latest_age_seconds is None or latest_age_seconds > self.settings.opera_volume_maximum_frame_age.total_seconds():
            state, error = "stale", "latest OPERA volume exceeds configured maximum age"
        else:
            state, error = "ready", None
        result = OperaVolumePollResult(
            state=state,
            fetched_at=fetched_at,
            latest_frame=latest_frame,
            latest_age_seconds=latest_age_seconds,
            discovered_frames=len(frames),
            archived_frames=archived_frames,
            existing_frames=existing_frames,
            track_profiles=tuple(track_profiles),
            frame_errors=tuple(frame_errors),
            error=error,
        )
        self.archive.write_opera_volume_status(result.as_dict(self.settings))
        return result
