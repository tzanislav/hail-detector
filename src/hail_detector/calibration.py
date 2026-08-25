from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Iterator

from PIL import Image

from .geospatial import LatLon, RadarImageBounds, YARLOVO_BOUNDS


RGB = tuple[int, int, int]


@dataclass(frozen=True)
class DbzBand:
    label: str
    lower_dbz: int
    upper_dbz: int


@dataclass(frozen=True)
class ReflectivityPixel:
    column: int
    row: int
    location: LatLon
    band: DbzBand


@dataclass(frozen=True)
class CalibrationSummary:
    width: int
    height: int
    transparent_pixels: int
    opaque_pixels: int
    classified_pixels: int
    band_counts: dict[str, int]
    unknown_opaque_colours: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


# These values were observed directly in the saved CAPPI PNGs and matched to
# the source legend. Higher classes deliberately stay unconfigured until a
# convective capture validates their raw PNG colours.
VALIDATED_COLOUR_BANDS: dict[RGB, DbzBand] = {
    (166, 247, 255): DbzBand("5-10", 5, 10),
    (69, 174, 250): DbzBand("10-15", 10, 15),
    (41, 215, 255): DbzBand("15-20", 15, 20),
    (36, 255, 36): DbzBand("20-25", 20, 25),
    (0, 192, 55): DbzBand("25-30", 25, 30),
    (0, 130, 39): DbzBand("30-35", 30, 35),
    (255, 255, 30): DbzBand("35-40", 35, 40),
    (255, 174, 0): DbzBand("40-45", 40, 45),
    (237, 0, 0): DbzBand("45-50", 45, 50),
}


def _open_rgba(image_bytes: bytes) -> Image.Image:
    image = Image.open(BytesIO(image_bytes))
    return image.convert("RGBA")


def _assert_dimensions(image: Image.Image, bounds: RadarImageBounds) -> None:
    if image.size != (bounds.width, bounds.height):
        raise ValueError(
            f"unexpected radar image size {image.size}; expected "
            f"{bounds.width}x{bounds.height}"
        )


def summarize_png(
    image_bytes: bytes, bounds: RadarImageBounds = YARLOVO_BOUNDS
) -> CalibrationSummary:
    """Return dBZ-band counts while preserving unknown opaque colours for audit."""

    image = _open_rgba(image_bytes)
    try:
        _assert_dimensions(image, bounds)
        band_counts: Counter[str] = Counter()
        unknown_colours: Counter[str] = Counter()
        transparent_pixels = 0
        opaque_pixels = 0

        pixels = image.tobytes()
        for offset in range(0, len(pixels), 4):
            red, green, blue, alpha = pixels[offset : offset + 4]
            if alpha == 0:
                transparent_pixels += 1
                continue

            opaque_pixels += 1
            band = VALIDATED_COLOUR_BANDS.get((red, green, blue))
            if band is None:
                unknown_colours[f"#{red:02X}{green:02X}{blue:02X}"] += 1
            else:
                band_counts[band.label] += 1

        return CalibrationSummary(
            width=image.width,
            height=image.height,
            transparent_pixels=transparent_pixels,
            opaque_pixels=opaque_pixels,
            classified_pixels=sum(band_counts.values()),
            band_counts=dict(sorted(band_counts.items())),
            unknown_opaque_colours=dict(sorted(unknown_colours.items())),
        )
    finally:
        image.close()


def iter_classified_pixels(
    image_bytes: bytes, bounds: RadarImageBounds = YARLOVO_BOUNDS
) -> Iterator[ReflectivityPixel]:
    """Yield georeferenced pixels in only the empirically validated dBZ bands."""

    image = _open_rgba(image_bytes)
    try:
        _assert_dimensions(image, bounds)
        for row in range(image.height):
            for column in range(image.width):
                red, green, blue, alpha = image.getpixel((column, row))
                if alpha == 0:
                    continue
                band = VALIDATED_COLOUR_BANDS.get((red, green, blue))
                if band is not None:
                    yield ReflectivityPixel(
                        column=column,
                        row=row,
                        location=bounds.pixel_to_latlon(column, row),
                        band=band,
                    )
    finally:
        image.close()
