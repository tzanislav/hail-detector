from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from collections import Counter
from math import floor
import re
from typing import Any
from urllib.parse import unquote, urlparse

import h5py
import httpx
import numpy as np
from PIL import Image
from pyproj import Transformer

from .alerting import AlertLifecycle
from .archive import ArchiveStore
from .config import Settings
from .geospatial import (
    LatLon,
    PixelCoordinate,
    RadarImageBounds,
    SOFIA_MAP_BOUNDS,
    _inverse_mercator_y,
    _mercator_y,
)
from .hail_policy import (
    HailAssessment,
    apply_llm_hail_risk,
    assess_track,
    suppress_for_unavailable_llm_research,
)
from .hail_risk_agent import HailRiskAgent, HailRiskResearchError
from .models import RadarFrame
from .storm_cells import DbzBand, PixelBounds, StormCell
from .telegram import TelegramDeliveryResult, TelegramNotifier
from .tracking import TrackManager
from .trajectory import project_track


DEFAULT_OPERA_API_URL = (
    "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/"
    "observations/locations/0-20010-0-OPERA"
)
OPERA_FILENAME_PATTERN = re.compile(
    r"^OPERA@(?P<timestamp>\d{8}T\d{4,6})@\d+@DBZH\.h5$", re.IGNORECASE
)


class OperaProtocolError(RuntimeError):
    """Raised when the OPERA API or an ODIM file has an unexpected shape."""


@dataclass(frozen=True)
class OperaSnapshot:
    payload: dict[str, Any]
    frames: tuple[RadarFrame, ...]
    fetched_at: datetime


@dataclass(frozen=True)
class OperaPollResult:
    state: str
    fetched_at: datetime
    latest_frame: RadarFrame | None
    latest_age_seconds: float | None
    discovered_frames: int
    archived_frames: int
    existing_frames: int
    frame_errors: tuple[str, ...]
    analyzed_frames: int = 0
    segmented_cells: int = 0
    qualified_storm_cells: int = 0
    active_tracks: int = 0
    forecasted_tracks: int = 0
    intersecting_target_tracks: int = 0
    earliest_intersection_at: str | None = None
    severe_alert_candidates: int = 0
    active_alerts: int = 0
    alert_transitions: int = 0
    notification_outbox_events: int = 0
    telegram_delivery_attempts: int = 0
    telegram_deliveries: int = 0
    telegram_delivery_failures: int = 0
    telegram_delivery_skipped_stale: int = 0
    llm_research_candidates: int = 0
    llm_research_requests: int = 0
    llm_research_successes: int = 0
    llm_research_failures: int = 0
    llm_research_cooldown_skips: int = 0
    alert_processing_deferred: bool = True
    analysis_errors: tuple[str, ...] = ()
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "EUMETNET OPERA maximum-reflectivity composite",
            "product": "DBZH maximum reflectivity",
            "state": self.state,
            "fetched_at": self.fetched_at.isoformat(),
            "latest_dbzh_composite": (
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
            "discovered_dbzh_composites": self.discovered_frames,
            "archived_frames": self.archived_frames,
            "existing_frames": self.existing_frames,
            "frame_errors": list(self.frame_errors),
            "analyzed_frames": self.analyzed_frames,
            "segmented_cells": self.segmented_cells,
            "qualified_storm_cells": self.qualified_storm_cells,
            "active_tracks": self.active_tracks,
            "forecasted_tracks": self.forecasted_tracks,
            "intersecting_target_tracks": self.intersecting_target_tracks,
            "earliest_intersection_at": self.earliest_intersection_at,
            "severe_alert_candidates": self.severe_alert_candidates,
            "active_alerts": self.active_alerts,
            "alert_transitions": self.alert_transitions,
            "notification_outbox_events": self.notification_outbox_events,
            "telegram_delivery_attempts": self.telegram_delivery_attempts,
            "telegram_deliveries": self.telegram_deliveries,
            "telegram_delivery_failures": self.telegram_delivery_failures,
            "telegram_delivery_skipped_stale": self.telegram_delivery_skipped_stale,
            "llm_research_candidates": self.llm_research_candidates,
            "llm_research_requests": self.llm_research_requests,
            "llm_research_successes": self.llm_research_successes,
            "llm_research_failures": self.llm_research_failures,
            "llm_research_cooldown_skips": self.llm_research_cooldown_skips,
            "alert_processing_deferred": self.alert_processing_deferred,
            "analysis_errors": list(self.analysis_errors),
            "error": self.error,
        }


def _filename_from_url(url: str) -> str:
    return unquote(urlparse(url).path.rsplit("/", maxsplit=1)[-1])


def _decode_attribute(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def parse_opera_composite_frames(payload: dict[str, Any]) -> tuple[RadarFrame, ...]:
    """Extract only DBZH ODIM composite download links from a CoverageJSON response."""

    links = payload.get("links")
    if not isinstance(links, list):
        raise OperaProtocolError("OPERA response has no links list")

    frames: list[RadarFrame] = []
    for item in links:
        if not isinstance(item, dict):
            continue
        url = item.get("href")
        content_type = item.get("type")
        if not isinstance(url, str) or content_type != "application/x-odim":
            continue
        filename = _filename_from_url(url)
        match = OPERA_FILENAME_PATTERN.fullmatch(filename)
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


class OperaClient:
    """Client for the public OPERA maximum-reflectivity composite API."""

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

    def __enter__(self) -> "OperaClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch_snapshot(self, now: datetime | None = None) -> OperaSnapshot:
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
                "method": "comp",
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise OperaProtocolError("OPERA response is not valid JSON") from error
        if not isinstance(payload, dict):
            raise OperaProtocolError("OPERA response root must be a JSON object")
        return OperaSnapshot(
            payload=payload,
            frames=parse_opera_composite_frames(payload),
            fetched_at=fetched_at,
        )

    def download_frame(self, frame: RadarFrame) -> bytes:
        response = self._client.get(frame.url)
        response.raise_for_status()
        if not response.content.startswith(b"\x89HDF\r\n\x1a\n"):
            raise OperaProtocolError(f"{frame.filename} is not an HDF5 response")
        return response.content


def _attribute_number(attributes: Any, name: str) -> float:
    try:
        return float(attributes[name])
    except KeyError as error:
        raise OperaProtocolError(f"OPERA ODIM metadata has no {name!r} attribute") from error


def _overlay_sample_coordinates(
    projection: str,
    xscale: float,
    yscale: float,
    xsize: int,
    ysize: int,
    bounds: RadarImageBounds,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return source-grid coordinates for an image placed exactly like Leaflet."""

    columns = np.arange(bounds.width, dtype=np.float64)
    rows = np.arange(bounds.height, dtype=np.float64)
    longitudes = bounds.west + (columns + 0.5) / bounds.width * (bounds.east - bounds.west)
    north_y = _mercator_y(bounds.north)
    south_y = _mercator_y(bounds.south)
    mercator_y = north_y + (rows + 0.5) / bounds.height * (south_y - north_y)
    latitudes = np.array([_inverse_mercator_y(value) for value in mercator_y])
    longitude_grid, latitude_grid = np.meshgrid(longitudes, latitudes)

    transformer = Transformer.from_crs("EPSG:4326", projection, always_xy=True)
    source_x, source_y = transformer.transform(longitude_grid, latitude_grid)
    # The OPERA composite's coordinate origin is its upper-left corner and y
    # decreases southwards. Sampling nearest cell centres avoids interpolation
    # that would invent reflectivity values at a cell boundary.
    source_columns = np.floor(source_x / xscale).astype(np.int64)
    source_rows = np.floor(-source_y / yscale).astype(np.int64)
    in_bounds = (
        np.isfinite(source_x)
        & np.isfinite(source_y)
        & (source_columns >= 0)
        & (source_columns < xsize)
        & (source_rows >= 0)
        & (source_rows < ysize)
    )
    return source_rows, source_columns, in_bounds


def _rgba_for_dbzh(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Use a deliberately labelled visual palette for the OPERA overlay."""

    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    palette = (
        (0, 10, (68, 129, 255, 72)),
        (10, 20, (34, 208, 255, 100)),
        (20, 35, (0, 209, 106, 128)),
        (35, 45, (242, 220, 0, 160)),
        (45, 55, (255, 126, 0, 194)),
        (55, float("inf"), (255, 45, 88, 230)),
    )
    for lower, upper, colour in palette:
        mask = valid & (values >= lower) & (values < upper)
        rgba[mask] = colour
    return rgba


def render_opera_overlay(
    content: bytes,
    bounds: RadarImageBounds = SOFIA_MAP_BOUNDS,
) -> tuple[bytes, dict[str, Any]]:
    """Crop and reproject an OPERA ODIM composite to the existing Sofia map bounds."""

    try:
        with h5py.File(BytesIO(content), "r") as source:
            what = source["what"].attrs
            where = source["where"].attrs
            data_group = source["dataset1/data1"]
            data_what = data_group["what"].attrs
            source_observed_timestamp = (
                _decode_attribute(what.get("date", ""))
                + "T"
                + _decode_attribute(what.get("time", ""))
            )
            quantity = _decode_attribute(data_what.get("quantity", ""))
            product = _decode_attribute(source["dataset1/what"].attrs.get("product", ""))
            if quantity != "DBZH" or product != "MAX":
                raise OperaProtocolError(
                    f"expected a DBZH maximum-reflectivity composite, got {quantity!r}/{product!r}"
                )

            projection = _decode_attribute(where.get("projdef", ""))
            if not projection:
                raise OperaProtocolError("OPERA ODIM metadata has no projection definition")
            xscale = _attribute_number(where, "xscale")
            yscale = _attribute_number(where, "yscale")
            xsize = int(_attribute_number(where, "xsize"))
            ysize = int(_attribute_number(where, "ysize"))
            dataset = data_group["data"]
            if dataset.shape != (ysize, xsize):
                raise OperaProtocolError(
                    f"OPERA grid shape {dataset.shape} does not match metadata {(ysize, xsize)}"
                )

            source_rows, source_columns, in_bounds = _overlay_sample_coordinates(
                projection, xscale, yscale, xsize, ysize, bounds
            )
            values = np.full((bounds.height, bounds.width), np.nan, dtype=np.float64)
            if np.any(in_bounds):
                row_min, row_max = source_rows[in_bounds].min(), source_rows[in_bounds].max()
                column_min, column_max = (
                    source_columns[in_bounds].min(),
                    source_columns[in_bounds].max(),
                )
                source_window = dataset[row_min : row_max + 1, column_min : column_max + 1]
                values[in_bounds] = source_window[
                    source_rows[in_bounds] - row_min,
                    source_columns[in_bounds] - column_min,
                ]

            nodata = _attribute_number(data_what, "nodata")
            undetect = _attribute_number(data_what, "undetect")
            gain = _attribute_number(data_what, "gain")
            offset = _attribute_number(data_what, "offset")
            valid = (
                in_bounds
                & np.isfinite(values)
                & ~np.isclose(values, nodata)
                & ~np.isclose(values, undetect)
            )
            values[valid] = values[valid] * gain + offset
            rgba = _rgba_for_dbzh(values, valid)
            image = Image.fromarray(rgba, mode="RGBA")
            try:
                encoded = BytesIO()
                image.save(encoded, format="PNG", optimize=True)
            finally:
                image.close()
    except (KeyError, OSError, ValueError) as error:
        raise OperaProtocolError(f"could not read OPERA ODIM composite: {error}") from error

    valid_values = values[valid]
    summary: dict[str, Any] = {
        "quantity": "DBZH",
        "product": "maximum reflectivity",
        "source_observed_timestamp": source_observed_timestamp,
        "source_grid": {
            "width": xsize,
            "height": ysize,
            "xscale_metres": xscale,
            "yscale_metres": yscale,
            "projection": projection,
        },
        "overlay_bounds": {
            "south": bounds.south,
            "west": bounds.west,
            "north": bounds.north,
            "east": bounds.east,
            "width": bounds.width,
            "height": bounds.height,
        },
        "valid_overlay_pixels": int(valid.sum()),
        "maximum_dbzh": float(valid_values.max()) if valid_values.size else None,
        "minimum_dbzh": float(valid_values.min()) if valid_values.size else None,
    }
    return encoded.getvalue(), summary


@dataclass(frozen=True)
class OperaGridBounds:
    """Physical OPERA composite grid used only for track-association gates."""

    xscale_metres: float = 1_000.0
    yscale_metres: float = 1_000.0

    def ground_resolution_at(self, _column: float, _row: float) -> tuple[float, float]:
        return self.xscale_metres, self.yscale_metres


OPERA_TRACKING_BOUNDS = OperaGridBounds()


def _dbzh_band(value: float) -> DbzBand:
    lower = max(0, int(floor(value / 5.0) * 5))
    return DbzBand(f"{lower}-{lower + 5}", lower, lower + 5)


def _neighbours(row: int, column: int) -> tuple[tuple[int, int], ...]:
    return tuple(
        (row + row_delta, column + column_delta)
        for row_delta in (-1, 0, 1)
        for column_delta in (-1, 0, 1)
        if row_delta or column_delta
    )


def _local_opera_window(
    projection: str,
    xscale: float,
    yscale: float,
    xsize: int,
    ysize: int,
    bounds: RadarImageBounds,
) -> tuple[int, int, int, int]:
    """Find a source-grid window that fully contains the map's geographic extent."""

    longitudes = np.linspace(bounds.west, bounds.east, 25)
    latitudes = np.linspace(bounds.south, bounds.north, 25)
    longitude_grid, latitude_grid = np.meshgrid(longitudes, latitudes)
    transformer = Transformer.from_crs("EPSG:4326", projection, always_xy=True)
    source_x, source_y = transformer.transform(longitude_grid, latitude_grid)
    columns = np.floor(source_x / xscale).astype(np.int64)
    rows = np.floor(-source_y / yscale).astype(np.int64)
    valid = (
        np.isfinite(source_x)
        & np.isfinite(source_y)
        & (columns >= 0)
        & (columns < xsize)
        & (rows >= 0)
        & (rows < ysize)
    )
    if not np.any(valid):
        raise OperaProtocolError("configured target map is outside the OPERA composite grid")
    # Include a one-pixel border because a geographic overlay edge is curved
    # relative to the OPERA LAEA grid.
    row_start = max(0, int(rows[valid].min()) - 1)
    row_end = min(ysize, int(rows[valid].max()) + 2)
    column_start = max(0, int(columns[valid].min()) - 1)
    column_end = min(xsize, int(columns[valid].max()) + 2)
    return row_start, row_end, column_start, column_end


def segment_opera_cells(
    content: bytes,
    frame: RadarFrame,
    *,
    minimum_dbz: int,
    minimum_pixels: int,
    centroid_weight_exponent: float = 3.0,
    bounds: RadarImageBounds = SOFIA_MAP_BOUNDS,
) -> list[StormCell]:
    """Segment OPERA's native 1 km DBZH grid in the local Sofia map extent.

    Unlike the displayed PNG, this works directly in the source grid so a
    single OPERA pixel remains one square kilometre rather than being expanded
    into multiple display pixels.
    """

    if minimum_dbz < 0:
        raise ValueError("minimum_dbz cannot be negative")
    if minimum_pixels <= 0:
        raise ValueError("minimum_pixels must be positive")
    if centroid_weight_exponent <= 0:
        raise ValueError("centroid_weight_exponent must be positive")
    try:
        with h5py.File(BytesIO(content), "r") as source:
            where = source["where"].attrs
            data_group = source["dataset1/data1"]
            data_what = data_group["what"].attrs
            quantity = _decode_attribute(data_what.get("quantity", ""))
            product = _decode_attribute(source["dataset1/what"].attrs.get("product", ""))
            if quantity != "DBZH" or product != "MAX":
                raise OperaProtocolError(
                    f"expected a DBZH maximum-reflectivity composite, got {quantity!r}/{product!r}"
                )
            projection = _decode_attribute(where.get("projdef", ""))
            if not projection:
                raise OperaProtocolError("OPERA ODIM metadata has no projection definition")
            xscale = _attribute_number(where, "xscale")
            yscale = _attribute_number(where, "yscale")
            xsize = int(_attribute_number(where, "xsize"))
            ysize = int(_attribute_number(where, "ysize"))
            dataset = data_group["data"]
            if dataset.shape != (ysize, xsize):
                raise OperaProtocolError(
                    f"OPERA grid shape {dataset.shape} does not match metadata {(ysize, xsize)}"
                )
            row_start, row_end, column_start, column_end = _local_opera_window(
                projection, xscale, yscale, xsize, ysize, bounds
            )
            values = np.asarray(dataset[row_start:row_end, column_start:column_end], dtype=np.float64)
            nodata = _attribute_number(data_what, "nodata")
            undetect = _attribute_number(data_what, "undetect")
            gain = _attribute_number(data_what, "gain")
            offset = _attribute_number(data_what, "offset")
    except (KeyError, OSError, ValueError) as error:
        raise OperaProtocolError(f"could not read OPERA ODIM composite: {error}") from error

    valid = (
        np.isfinite(values)
        & ~np.isclose(values, nodata)
        & ~np.isclose(values, undetect)
    )
    values[valid] = values[valid] * gain + offset
    eligible = valid & (values >= minimum_dbz)
    seen: set[tuple[int, int]] = set()
    components: list[list[tuple[int, int]]] = []
    for seed_row, seed_column in zip(*np.nonzero(eligible)):
        seed = int(seed_row), int(seed_column)
        if seed in seen:
            continue
        seen.add(seed)
        component: list[tuple[int, int]] = []
        stack = [seed]
        while stack:
            row, column = stack.pop()
            component.append((row, column))
            for neighbour in _neighbours(row, column):
                neighbour_row, neighbour_column = neighbour
                if (
                    0 <= neighbour_row < eligible.shape[0]
                    and 0 <= neighbour_column < eligible.shape[1]
                    and eligible[neighbour_row, neighbour_column]
                    and neighbour not in seen
                ):
                    seen.add(neighbour)
                    stack.append(neighbour)
        if len(component) >= minimum_pixels:
            components.append(component)

    components.sort(key=lambda component: min(component))
    inverse = Transformer.from_crs(projection, "EPSG:4326", always_xy=True)
    cells: list[StormCell] = []
    for index, component in enumerate(components, start=1):
        rows, columns = zip(*component)
        global_rows = np.array(rows, dtype=np.float64) + row_start
        global_columns = np.array(columns, dtype=np.float64) + column_start
        cell_values = np.array([values[row, column] for row, column in component])
        # dBZ is logarithmic, so do not cube its absolute value.  Instead,
        # every eligible pixel receives a positive weight based on its dBZ
        # excess above the segmentation floor.  Cubing that excess makes the
        # centroid follow the strongest embedded core while retaining a small
        # contribution from the rest of the connected echo.
        weights = np.power(
            np.maximum(1.0, cell_values - minimum_dbz + 1.0),
            centroid_weight_exponent,
        )
        centroid_row = float(np.average(global_rows, weights=weights))
        centroid_column = float(np.average(global_columns, weights=weights))
        longitude, latitude = inverse.transform(
            (centroid_column + 0.5) * xscale,
            -(centroid_row + 0.5) * yscale,
        )
        bands = [_dbzh_band(value) for value in cell_values]
        max_band = _dbzh_band(float(cell_values.max()))
        counts = Counter(band.label for band in bands)
        cell_area = xscale * yscale / 1_000_000
        cells.append(
            StormCell(
                cell_id=f"{frame.filename}#{index:03d}",
                frame_filename=frame.filename,
                observed_at=frame.observed_at,
                centroid_pixel=PixelCoordinate(column=centroid_column, row=centroid_row),
                centroid_location=LatLon(latitude=latitude, longitude=longitude),
                pixel_count=len(component),
                area_square_km=len(component) * cell_area,
                area_at_or_above_square_km={
                    f">={threshold}": float((cell_values >= threshold).sum()) * cell_area
                    for threshold in (35, 45, 55, 60, 65)
                },
                max_band=max_band,
                band_pixel_counts=dict(sorted(counts.items())),
                pixel_bounds=PixelBounds(
                    min_column=int(global_columns.min()),
                    min_row=int(global_rows.min()),
                    max_column=int(global_columns.max()),
                    max_row=int(global_rows.max()),
                ),
            )
        )
    return cells


def qualify_opera_storm_cell(cell: StormCell, settings: Settings) -> tuple[bool, str]:
    """Decide whether a raw reflectivity component is operationally storm-like.

    The segmentation threshold is deliberately sensitive so the raw archive
    retains weak echoes.  Tracking needs a more selective gate: either a
    broad component with a substantial 45 dBZ core, or a compact 55 dBZ core
    that can be important despite its small footprint.  This is an echo
    quality filter, not a hail decision.
    """

    core_45 = cell.area_at_or_above_square_km.get(">=45", 0.0)
    core_55 = cell.area_at_or_above_square_km.get(">=55", 0.0)
    if core_55 >= settings.opera_storm_minimum_55dbz_core_square_km:
        return True, "contains a compact >=55 dBZ core"
    if (
        cell.area_square_km >= settings.opera_storm_minimum_area_square_km
        and core_45 >= settings.opera_storm_minimum_45dbz_core_square_km
    ):
        return True, "has organised area and a substantial >=45 dBZ core"
    return (
        False,
        "below operational storm gate "
        f"(area {cell.area_square_km:.1f} km²; >=45 dBZ core {core_45:.1f} km²; "
        f">=55 dBZ core {core_55:.1f} km²)",
    )


@dataclass(frozen=True)
class OperaAnalysisResult:
    processed_frames: int
    segmented_cells: int
    qualified_storm_cells: int
    active_tracks: int
    forecasted_tracks: int
    intersecting_target_tracks: int
    earliest_intersection_at: str | None
    assessments: tuple[HailAssessment, ...]
    errors: tuple[str, ...]


class OperaAnalysisPipeline:
    """Track local OPERA DBZH cells without assigning hail severity."""

    def __init__(self, settings: Settings, archive: ArchiveStore):
        self.settings = settings
        self.archive = archive

    def _centroid_weighting(self) -> dict[str, object]:
        return {
            "method": "dbzh_excess_power",
            "minimum_dbz": self.settings.opera_segmentation_minimum_dbz,
            "exponent": self.settings.opera_centroid_weight_exponent,
        }

    def process(self, frames: tuple[RadarFrame, ...]) -> OperaAnalysisResult:
        centroid_weighting = self._centroid_weighting()
        stored_state = self.archive.read_opera_tracker_state()
        # A changed centroid method invalidates historic centroid locations.
        # Rebuild the lookback window instead of mixing old geometric points
        # with new intensity-weighted points in one motion fit.
        state_to_restore = (
            stored_state
            if stored_state
            and stored_state.get("centroid_weighting") == centroid_weighting
            else None
        )
        manager = TrackManager.from_dict(
            state_to_restore,
            maximum_gap=self.settings.tracker_maximum_gap,
            maximum_speed_kmh=self.settings.tracker_maximum_speed_kmh,
            bounds=OPERA_TRACKING_BOUNDS,  # type: ignore[arg-type]
            motion_window_observations=self.settings.tracker_motion_window_observations,
        )
        processed_frames = 0
        segmented_cells = 0
        qualified_storm_cells = 0
        errors: list[str] = []
        for frame in sorted(frames, key=lambda item: item.observed_at):
            if manager.last_processed_at and frame.observed_at <= manager.last_processed_at:
                continue
            raw_path = self.archive.opera_raw_path(frame)
            if not raw_path.is_file():
                errors.append(f"{frame.filename}: raw OPERA composite is missing from the archive")
                break
            try:
                raw_cells = segment_opera_cells(
                    raw_path.read_bytes(),
                    frame,
                    minimum_dbz=self.settings.opera_segmentation_minimum_dbz,
                    minimum_pixels=self.settings.opera_segmentation_minimum_pixels,
                    centroid_weight_exponent=self.settings.opera_centroid_weight_exponent,
                )
                cells: list[StormCell] = []
                rejected_cells: list[dict[str, object]] = []
                for cell in raw_cells:
                    qualified, reason = qualify_opera_storm_cell(cell, self.settings)
                    if qualified:
                        cells.append(cell)
                    else:
                        rejected_cells.append(cell.as_dict() | {"rejection_reason": reason})
                self.archive.write_opera_cells(
                    frame,
                    [cell.as_dict() for cell in cells],
                    rejected_cells,
                    centroid_weighting,
                )
                manager.associate_frame(frame.observed_at, cells)
            except Exception as error:
                errors.append(f"{frame.filename}: {error}")
                break
            processed_frames += 1
            segmented_cells += len(raw_cells)
            qualified_storm_cells += len(cells)

        # A poll can contain only already-processed frames.  Still retire
        # echoes that have disappeared since the last successful analysis.
        if frames:
            manager.prune_stale_tracks(max(frame.observed_at for frame in frames))

        confirmed_tracks = [
            track
            for track in manager.tracks.values()
            if len(track.points) >= self.settings.opera_track_minimum_confirmations
        ]

        forecasts = [
            project_track(
                track,
                self.settings.target,
                self.settings.forecast_horizon,
                motion_window_observations=self.settings.tracker_motion_window_observations,
            )
            for track in confirmed_tracks
        ]
        intersections = [
            forecast
            for forecast in forecasts
            if forecast.intersects_target_radius and forecast.arrival_window_start is not None
        ]
        earliest = min((item.arrival_window_start for item in intersections), default=None)
        state = manager.as_dict()
        state["source"] = "eumetnet_opera_dbzh_maximum_reflectivity"
        state["target"] = self.settings.target.as_dict()
        state["forecast_horizon_minutes"] = int(self.settings.forecast_horizon.total_seconds() / 60)
        state["centroid_weighting"] = centroid_weighting
        state["minimum_confirmed_observations"] = self.settings.opera_track_minimum_confirmations
        state["confirmed_track_count"] = len(confirmed_tracks)
        state["pending_track_count"] = len(manager.tracks) - len(confirmed_tracks)
        state["forecasts"] = [forecast.as_dict() for forecast in forecasts]
        assessments = tuple(
            assess_track(track, forecast, self.settings.severe_alert)
            for track, forecast in zip(confirmed_tracks, forecasts)
        )
        self.archive.write_assessments(
            [assessment.as_dict() for assessment in assessments],
            self.settings.target.as_dict(),
        )
        state["severe_alert_policy"] = {
            "minimum_dbz": self.settings.severe_alert.minimum_dbz,
            "maximum_lead_minutes": self.settings.severe_alert.maximum_lead_minutes,
            "minimum_trajectory_confidence": (
                self.settings.severe_alert.minimum_trajectory_confidence
            ),
        }
        self.archive.write_opera_tracker_state(state)
        return OperaAnalysisResult(
            processed_frames=processed_frames,
            segmented_cells=segmented_cells,
            qualified_storm_cells=qualified_storm_cells,
            active_tracks=len(confirmed_tracks),
            forecasted_tracks=sum(item.available for item in forecasts),
            intersecting_target_tracks=len(intersections),
            earliest_intersection_at=earliest.isoformat() if earliest else None,
            assessments=assessments,
            errors=tuple(errors),
        )


class OperaIngestionService:
    """Archive, track, assess, and alert from the primary OPERA composite."""

    def __init__(
        self,
        settings: Settings,
        archive: ArchiveStore,
        client: OperaClient,
        analysis: OperaAnalysisPipeline | None = None,
        alert_lifecycle: AlertLifecycle | None = None,
        telegram_notifier: TelegramNotifier | None = None,
        hail_risk_agent: HailRiskAgent | None = None,
    ):
        self.settings = settings
        self.archive = archive
        self.client = client
        self.analysis = analysis or OperaAnalysisPipeline(settings, archive)
        self.alert_lifecycle = alert_lifecycle or AlertLifecycle(
            archive, settings.alert_lifecycle.expiry
        )
        self.telegram_notifier = telegram_notifier
        self.hail_risk_agent = hail_risk_agent

    def _volume_profile_for(self, track_id: str) -> dict[str, Any] | None:
        """Return the optional experimental profile for this exact primary track."""

        volume_status = self.archive.read_opera_volume_status()
        if not isinstance(volume_status, dict):
            return None
        profiles = volume_status.get("track_profiles")
        if not isinstance(profiles, list):
            return None
        for profile in profiles:
            if isinstance(profile, dict) and profile.get("track_id") == track_id:
                return profile
        return None

    def _research_assessments(
        self,
        assessments: tuple[HailAssessment, ...],
        *,
        fetched_at: datetime,
    ) -> tuple[tuple[HailAssessment, ...], int, int, int, int, int, tuple[str, ...]]:
        """Apply the optional LLM gate and persist a per-track request cooldown."""

        if self.hail_risk_agent is None:
            return assessments, 0, 0, 0, 0, 0, ()

        updated: list[HailAssessment] = []
        candidates = requests = successes = failures = cooldown_skips = 0
        errors: list[str] = []
        for assessment in assessments:
            value = assessment.as_dict()
            if not self.hail_risk_agent.is_candidate(value):
                if assessment.candidate_level is not None:
                    updated.append(
                        suppress_for_unavailable_llm_research(
                            assessment,
                            "the track does not meet the configured web-research request criteria",
                        )
                    )
                else:
                    updated.append(assessment)
                continue
            candidates += 1
            previous = self.archive.read_hail_risk_research_record(assessment.track_id)
            requested_at = previous.get("requested_at") if previous else None
            previous_request_at: datetime | None = None
            if isinstance(requested_at, str):
                try:
                    previous_request_at = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
                    if previous_request_at.tzinfo is None:
                        previous_request_at = previous_request_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    previous_request_at = None
            if (
                previous_request_at is not None
                and fetched_at - previous_request_at < self.settings.hail_risk_agent.research_cooldown
            ):
                cooldown_skips += 1
                cached_research = previous.get("research") if previous else None
                if isinstance(cached_research, dict):
                    try:
                        updated.append(
                            apply_llm_hail_risk(
                                assessment,
                                cached_research,
                                alert_threshold=self.settings.hail_risk_agent.alert_threshold,
                            )
                        )
                    except ValueError as error:
                        errors.append(f"{assessment.track_id}: cached web research is invalid: {error}")
                        updated.append(
                            suppress_for_unavailable_llm_research(
                                assessment,
                                "the previous web-research result is invalid and its cooldown is active",
                            )
                        )
                else:
                    updated.append(
                        suppress_for_unavailable_llm_research(
                            assessment,
                            "a previous web-research request did not produce a usable result and its cooldown is active",
                        )
                    )
                continue

            attempt: dict[str, Any] = {"requested_at": fetched_at.isoformat()}
            try:
                # Record this before invoking OpenAI. A timeout can still mean the
                # request reached the API, so it must begin the cooldown as well.
                self.archive.write_hail_risk_research_record(assessment.track_id, attempt)
            except (OSError, TypeError, ValueError) as error:
                errors.append(
                    f"{assessment.track_id}: could not persist web-research cooldown: {error}"
                )
                updated.append(suppress_for_unavailable_llm_research(assessment, str(error)))
                failures += 1
                continue
            requests += 1
            try:
                research = self.hail_risk_agent.assess(
                    value,
                    target=self.settings.target,
                    volume_profile=self._volume_profile_for(assessment.track_id),
                    now=fetched_at,
                )
                self.archive.write_hail_risk_research_record(
                    assessment.track_id,
                    attempt | {"research": research.as_dict()},
                )
                updated.append(
                    apply_llm_hail_risk(
                        assessment,
                        research.as_dict(),
                        alert_threshold=self.settings.hail_risk_agent.alert_threshold,
                    )
                )
                successes += 1
            except (HailRiskResearchError, ValueError) as error:
                # An enabled research gate must not fall back to the earlier
                # reflectivity-only candidate; that would bypass its threshold.
                errors.append(f"{assessment.track_id}: {error}")
                self.archive.write_hail_risk_research_record(
                    assessment.track_id,
                    attempt | {"error": str(error)},
                )
                updated.append(suppress_for_unavailable_llm_research(assessment, str(error)))
                failures += 1
        return (
            tuple(updated),
            candidates,
            requests,
            successes,
            failures,
            cooldown_skips,
            tuple(errors),
        )

    def _write_error(self, fetched_at: datetime, error: Exception) -> OperaPollResult:
        self.archive.prune_frames_older_than(fetched_at - self.settings.frame_retention)
        result = OperaPollResult(
            state="error",
            fetched_at=fetched_at,
            latest_frame=None,
            latest_age_seconds=None,
            discovered_frames=0,
            archived_frames=0,
            existing_frames=0,
            frame_errors=(),
            error=str(error),
        )
        self.archive.write_opera_status(result.as_dict())
        return result

    def poll_once(self, now: datetime | None = None) -> OperaPollResult:
        fetched_at = now or datetime.now(timezone.utc)
        if fetched_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        try:
            snapshot = self.client.fetch_snapshot(fetched_at)
            fetched_at = snapshot.fetched_at
            self.archive.archive_opera_manifest(snapshot.payload, fetched_at)
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
                raw_exists = self.archive.has_opera_raw_frame(frame)
                if raw_exists:
                    existing_frames += 1
                else:
                    content = self.client.download_frame(frame)
                    self.archive.archive_opera_frame(frame, content, fetched_at)
                    archived_frames += 1
                if not self.archive.has_opera_overlay(frame):
                    content = self.archive.opera_raw_path(frame).read_bytes()
                    overlay, summary = render_opera_overlay(content)
                    self.archive.archive_opera_overlay(frame, overlay, summary)
            except Exception as error:
                frame_errors.append(f"{frame.filename}: {error}")

        latest_is_archived = latest_frame is not None and self.archive.has_opera_raw_frame(latest_frame)
        latest_has_overlay = latest_frame is not None and self.archive.has_opera_overlay(latest_frame)
        analysis_result = self.analysis.process(frames)
        alert_data_is_fresh = (
            latest_is_archived
            and latest_has_overlay
            and latest_age_seconds is not None
            and latest_age_seconds <= self.settings.opera_maximum_frame_age.total_seconds()
            and not frame_errors
            and not analysis_result.errors
        )
        assessments = analysis_result.assessments
        research_candidates = research_requests = research_successes = research_failures = 0
        research_cooldown_skips = 0
        research_errors: tuple[str, ...] = ()
        if alert_data_is_fresh:
            (
                assessments,
                research_candidates,
                research_requests,
                research_successes,
                research_failures,
                research_cooldown_skips,
                research_errors,
            ) = self._research_assessments(assessments, fetched_at=fetched_at)
        alert_processing_data_is_fresh = alert_data_is_fresh and research_failures == 0
        self.archive.write_assessments(
            [assessment.as_dict() for assessment in assessments],
            self.settings.target.as_dict(),
        )
        alert_result = self.alert_lifecycle.reconcile(
            [assessment.as_dict() for assessment in assessments],
            evaluated_at=fetched_at,
            data_is_fresh=alert_processing_data_is_fresh,
        )
        delivery_result = (
            self.telegram_notifier.deliver_pending(self.archive, now=fetched_at)
            if self.telegram_notifier is not None and alert_processing_data_is_fresh
            else TelegramDeliveryResult()
        )
        self.archive.prune_frames_older_than(fetched_at - self.settings.frame_retention)
        if not frames:
            state, error = "error", "OPERA response contains no DBZH composite links"
        elif not latest_is_archived or not latest_has_overlay:
            state, error = "error", "latest OPERA composite could not be archived and rendered"
        elif analysis_result.errors:
            state, error = "error", "OPERA cell analysis could not process all chronological composites"
        elif frame_errors:
            state, error = "degraded", "one or more OPERA composites could not be archived or rendered"
        elif latest_age_seconds is None or latest_age_seconds > self.settings.opera_maximum_frame_age.total_seconds():
            state, error = "stale", "latest OPERA composite exceeds configured maximum age"
        else:
            state, error = "ready", None
        result = OperaPollResult(
            state=state,
            fetched_at=fetched_at,
            latest_frame=latest_frame,
            latest_age_seconds=latest_age_seconds,
            discovered_frames=len(frames),
            archived_frames=archived_frames,
            existing_frames=existing_frames,
            frame_errors=tuple(frame_errors),
            analyzed_frames=analysis_result.processed_frames,
            segmented_cells=analysis_result.segmented_cells,
            qualified_storm_cells=analysis_result.qualified_storm_cells,
            active_tracks=analysis_result.active_tracks,
            forecasted_tracks=analysis_result.forecasted_tracks,
            intersecting_target_tracks=analysis_result.intersecting_target_tracks,
            earliest_intersection_at=analysis_result.earliest_intersection_at,
            severe_alert_candidates=sum(assessment.candidate_level is not None for assessment in assessments),
            active_alerts=alert_result.active_alerts,
            alert_transitions=len(alert_result.transitions),
            notification_outbox_events=alert_result.outbox_events,
            telegram_delivery_attempts=delivery_result.attempted,
            telegram_deliveries=delivery_result.delivered,
            telegram_delivery_failures=delivery_result.failed,
            telegram_delivery_skipped_stale=delivery_result.skipped_stale,
            llm_research_candidates=research_candidates,
            llm_research_requests=research_requests,
            llm_research_successes=research_successes,
            llm_research_failures=research_failures,
            llm_research_cooldown_skips=research_cooldown_skips,
            alert_processing_deferred=alert_result.deferred_for_stale_data,
            analysis_errors=(*analysis_result.errors, *research_errors),
            error=error,
        )
        self.archive.write_opera_status(result.as_dict())
        return result
