from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable

from .calibration import DbzBand, ReflectivityPixel, iter_classified_pixels
from .geospatial import LatLon, PixelCoordinate, RadarImageBounds, YARLOVO_BOUNDS
from .models import RadarFrame


@dataclass(frozen=True)
class PixelBounds:
    min_column: int
    min_row: int
    max_column: int
    max_row: int


@dataclass(frozen=True)
class StormCell:
    """One 8-connected group of qualifying radar pixels in a single frame."""

    cell_id: str
    frame_filename: str
    observed_at: datetime
    centroid_pixel: PixelCoordinate
    centroid_location: LatLon
    pixel_count: int
    area_square_km: float
    area_at_or_above_square_km: dict[str, float]
    max_band: DbzBand
    band_pixel_counts: dict[str, int]
    pixel_bounds: PixelBounds

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["observed_at"] = self.observed_at.isoformat()
        return data


def _neighbours(row: int, column: int) -> Iterable[tuple[int, int]]:
    for row_delta in (-1, 0, 1):
        for column_delta in (-1, 0, 1):
            if row_delta or column_delta:
                yield row + row_delta, column + column_delta


def _eligible_pixels(
    image_bytes: bytes, minimum_dbz: int, bounds: RadarImageBounds
) -> dict[tuple[int, int], DbzBand]:
    return {
        (pixel.row, pixel.column): pixel.band
        for pixel in iter_classified_pixels(image_bytes, bounds)
        # A class is included only when its documented lower bound reaches the
        # threshold. This avoids claiming that an interval partly below it does.
        if pixel.band.lower_dbz >= minimum_dbz
    }


def segment_cells(
    image_bytes: bytes,
    frame: RadarFrame,
    *,
    minimum_dbz: int,
    minimum_pixels: int,
    bounds: RadarImageBounds = YARLOVO_BOUNDS,
) -> list[StormCell]:
    """Segment 8-connected radar cores into deterministic, georeferenced cells."""

    if minimum_dbz < 0:
        raise ValueError("minimum_dbz cannot be negative")
    if minimum_pixels <= 0:
        raise ValueError("minimum_pixels must be positive")

    pixels = _eligible_pixels(image_bytes, minimum_dbz, bounds)
    seen: set[tuple[int, int]] = set()
    components: list[list[tuple[int, int]]] = []

    for seed in pixels:
        if seed in seen:
            continue
        seen.add(seed)
        component: list[tuple[int, int]] = []
        stack = [seed]
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in _neighbours(*current):
                if neighbour in pixels and neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        if len(component) >= minimum_pixels:
            components.append(component)

    components.sort(key=lambda component: min(component))
    cells: list[StormCell] = []
    for index, component in enumerate(components, start=1):
        rows, columns = zip(*component)
        centroid_pixel = PixelCoordinate(
            column=sum(columns) / len(columns), row=sum(rows) / len(rows)
        )
        horizontal_resolution, vertical_resolution = bounds.ground_resolution_at(
            centroid_pixel.column, centroid_pixel.row
        )
        bands = [pixels[coordinate] for coordinate in component]
        max_band = max(bands, key=lambda band: (band.lower_dbz, band.upper_dbz))
        counts = Counter(band.label for band in bands)
        area_at_or_above = {
            f">={threshold}": sum(
                band.lower_dbz >= threshold for band in bands
            )
            * horizontal_resolution
            * vertical_resolution
            / 1_000_000
            for threshold in (35, 45, 55, 60, 65)
        }
        cells.append(
            StormCell(
                cell_id=f"{frame.filename}#{index:03d}",
                frame_filename=frame.filename,
                observed_at=frame.observed_at,
                centroid_pixel=centroid_pixel,
                centroid_location=bounds.pixel_to_latlon(
                    centroid_pixel.column, centroid_pixel.row
                ),
                pixel_count=len(component),
                area_square_km=len(component)
                * horizontal_resolution
                * vertical_resolution
                / 1_000_000,
                area_at_or_above_square_km=area_at_or_above,
                max_band=max_band,
                band_pixel_counts=dict(sorted(counts.items())),
                pixel_bounds=PixelBounds(
                    min_column=min(columns),
                    min_row=min(rows),
                    max_column=max(columns),
                    max_row=max(rows),
                ),
            )
        )
    return cells
