from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from .geospatial import LatLon, PixelCoordinate


@dataclass(frozen=True)
class DbzBand:
    label: str
    lower_dbz: int
    upper_dbz: int


@dataclass(frozen=True)
class PixelBounds:
    min_column: int
    min_row: int
    max_column: int
    max_row: int


@dataclass(frozen=True)
class StormCell:
    """One segmented reflectivity component from an OPERA composite."""

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
