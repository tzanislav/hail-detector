from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

from hail_detector.yarlovo import parse_cappi_frames


FIXTURES = Path(__file__).resolve().parents[1] / "research" / "yarlovo" / "frames"


def test_active_manifest_filters_non_cappi_product_and_sorts_by_precise_timestamp() -> None:
    payload = json.loads((FIXTURES / "JAR.manifest.json").read_text(encoding="utf-8"))

    frames = parse_cappi_frames(payload, ZoneInfo("Europe/Sofia"))

    assert len(frames) == 13
    assert frames[0].filename == "JAR260825063311.CAP20X3.png"
    assert frames[-1].filename == "JAR260825071742.CAP20Z8.png"
    assert frames[-1].observed_at.isoformat() == "2026-08-25T07:17:42+03:00"
    assert all(".CAP" in frame.filename for frame in frames)
