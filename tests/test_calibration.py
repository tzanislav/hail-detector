from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from hail_detector.calibration import iter_classified_pixels, summarize_png


FIXTURES = Path(__file__).resolve().parents[1] / "research" / "yarlovo" / "frames"


def test_saved_png_has_only_validated_opaque_colours() -> None:
    content = (FIXTURES / "JAR260825071742.CAP20Z8.png").read_bytes()

    summary = summarize_png(content)

    assert (summary.width, summary.height) == (720, 720)
    assert summary.transparent_pixels == 518_247
    assert summary.opaque_pixels == 153
    assert summary.classified_pixels == 153
    assert summary.unknown_opaque_colours == {}
    assert summary.band_counts == {
        "5-10": 62,
        "10-15": 13,
        "15-20": 69,
        "20-25": 5,
        "35-40": 4,
    }


def test_classified_pixels_are_georeferenced() -> None:
    content = (FIXTURES / "JAR260825071742.CAP20Z8.png").read_bytes()

    pixels = list(iter_classified_pixels(content))

    assert len(pixels) == 153
    assert all(40.674 <= pixel.location.latitude <= 44.455 for pixel in pixels)
    assert all(20.678 <= pixel.location.longitude <= 25.822 for pixel in pixels)


def test_unknown_opaque_colour_is_audited_not_assigned_a_dbz_band() -> None:
    image = Image.new("RGBA", (720, 720), (0, 0, 0, 0))
    image.putpixel((12, 34), (99, 0, 255, 255))
    encoded = BytesIO()
    image.save(encoded, format="PNG")

    summary = summarize_png(encoded.getvalue())

    assert summary.opaque_pixels == 1
    assert summary.classified_pixels == 0
    assert summary.unknown_opaque_colours == {"#6300FF": 1}
