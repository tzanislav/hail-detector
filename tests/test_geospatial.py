from __future__ import annotations

import pytest

from hail_detector.geospatial import SOFIA_MAP_BOUNDS


def test_sofia_city_centre_maps_to_the_documented_overlay_pixel() -> None:
    pixel = SOFIA_MAP_BOUNDS.latlon_to_pixel(42.6977, 23.3219)

    assert pixel.column == pytest.approx(370.064, abs=0.01)
    assert pixel.row == pytest.approx(340.077, abs=0.01)


def test_pixel_round_trip_uses_web_mercator_y() -> None:
    source = SOFIA_MAP_BOUNDS.pixel_to_latlon(370, 340)
    destination = SOFIA_MAP_BOUNDS.latlon_to_pixel(source.latitude, source.longitude)

    assert destination.column == pytest.approx(370.5, abs=1e-6)
    assert destination.row == pytest.approx(340.5, abs=1e-6)
    assert SOFIA_MAP_BOUNDS.web_mercator_metres_per_pixel_x == pytest.approx(795.3, abs=0.1)
    assert SOFIA_MAP_BOUNDS.web_mercator_metres_per_pixel_y == pytest.approx(794.1, abs=0.1)


def test_ground_resolution_accounts_for_sofia_latitude() -> None:
    horizontal, vertical = SOFIA_MAP_BOUNDS.ground_resolution_at(370, 340)

    assert horizontal == pytest.approx(586.0, abs=2.0)
    assert vertical == pytest.approx(585.0, abs=2.0)
