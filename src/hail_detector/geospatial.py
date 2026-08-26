from __future__ import annotations

from dataclasses import dataclass
from math import asin, atan, cos, exp, log, pi, radians, sin, sqrt, tan


EARTH_RADIUS_M = 6_378_137.0


def _mercator_y(latitude: float) -> float:
    latitude_radians = radians(latitude)
    return EARTH_RADIUS_M * log(tan(pi / 4 + latitude_radians / 2))


def _inverse_mercator_y(y_metres: float) -> float:
    return (2 * atan(exp(y_metres / EARTH_RADIUS_M)) - pi / 2) * 180 / pi


@dataclass(frozen=True)
class LatLon:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class PixelCoordinate:
    column: float
    row: float


@dataclass(frozen=True)
class RadarImageBounds:
    """Leaflet image-overlay bounds for the Sofia monitoring area."""

    south: float = 40.674
    west: float = 20.678
    north: float = 44.455
    east: float = 25.822
    width: int = 720
    height: int = 720

    @property
    def web_mercator_metres_per_pixel_x(self) -> float:
        """Projected overlay resolution, not physical east/west ground distance."""

        west_x = EARTH_RADIUS_M * radians(self.west)
        east_x = EARTH_RADIUS_M * radians(self.east)
        return (east_x - west_x) / self.width

    @property
    def web_mercator_metres_per_pixel_y(self) -> float:
        """Projected overlay resolution, not physical north/south ground distance."""

        return (_mercator_y(self.north) - _mercator_y(self.south)) / self.height

    def pixel_to_latlon(self, column: float, row: float) -> LatLon:
        """Map a pixel centre/centroid to the same geography as Leaflet's overlay.

        Rows must be transformed in Web Mercator because Leaflet projects the
        north/south bounds before rendering the image, so latitude is not linear
        along the image height.
        """

        if not 0 <= column < self.width or not 0 <= row < self.height:
            raise ValueError("pixel must fall within the radar image")

        x_fraction = (column + 0.5) / self.width
        y_fraction = (row + 0.5) / self.height
        longitude = self.west + x_fraction * (self.east - self.west)

        north_y = _mercator_y(self.north)
        south_y = _mercator_y(self.south)
        mercator_y = north_y + y_fraction * (south_y - north_y)
        return LatLon(latitude=_inverse_mercator_y(mercator_y), longitude=longitude)

    def latlon_to_pixel(self, latitude: float, longitude: float) -> PixelCoordinate:
        """Map a coordinate to a floating-point image pixel position."""

        if not self.south <= latitude <= self.north or not self.west <= longitude <= self.east:
            raise ValueError("coordinate falls outside the radar image bounds")

        column = (longitude - self.west) / (self.east - self.west) * self.width
        north_y = _mercator_y(self.north)
        south_y = _mercator_y(self.south)
        row = (north_y - _mercator_y(latitude)) / (north_y - south_y) * self.height
        return PixelCoordinate(column=column, row=row)

    def ground_resolution_at(self, column: float, row: float) -> tuple[float, float]:
        """Return physical east/west and north/south metres represented by one pixel.

        Image placement happens in Web Mercator, but cell area and storm speed
        must use geodesic distances at the cell's latitude.
        """

        point = self.pixel_to_latlon(column, row)
        neighbouring_column = column + 1 if column + 1 < self.width else column - 1
        neighbouring_row = row + 1 if row + 1 < self.height else row - 1
        east_west = _haversine_metres(point, self.pixel_to_latlon(neighbouring_column, row))
        north_south = _haversine_metres(point, self.pixel_to_latlon(column, neighbouring_row))
        return east_west, north_south


def _haversine_metres(first: LatLon, second: LatLon) -> float:
    latitude_delta = radians(second.latitude - first.latitude)
    longitude_delta = radians(second.longitude - first.longitude)
    first_latitude = radians(first.latitude)
    second_latitude = radians(second.latitude)
    a = sin(latitude_delta / 2) ** 2 + cos(first_latitude) * cos(second_latitude) * sin(
        longitude_delta / 2
    ) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(a))


SOFIA_MAP_BOUNDS = RadarImageBounds()
