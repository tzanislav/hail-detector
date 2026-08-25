from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

from hail_detector.analysis import StormAnalysisPipeline
from hail_detector.archive import ArchiveStore
from hail_detector.config import Settings
from hail_detector.models import RadarFrame


TIMEZONE = ZoneInfo("Europe/Sofia")


def _settings(root: Path) -> Settings:
    return Settings(
        archive_root=root,
        manifest_url="https://example.test/JAR.json",
        source_timezone=TIMEZONE,
        maximum_frame_age=timedelta(minutes=10),
        poll_interval_seconds=240,
        segmentation_minimum_dbz=35,
        segmentation_minimum_pixels=4,
        tracker_maximum_gap=timedelta(minutes=12),
        tracker_maximum_speed_kmh=160,
    )


def _frame(observed_at: datetime) -> RadarFrame:
    filename = f"JAR{observed_at.strftime('%y%m%d%H%M%S')}.CAPTEST.png"
    return RadarFrame(
        filename=filename,
        url=f"https://example.test/{filename}",
        observed_at=observed_at,
        display_timestamp=observed_at.strftime("%d.%m.%Y %H:%M"),
    )


def _core_png(column: int, row: int) -> bytes:
    image = Image.new("RGBA", (720, 720), (0, 0, 0, 0))
    for x in (column, column + 1):
        for y in (row, row + 1):
            image.putpixel((x, y), (255, 255, 30, 255))
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    return encoded.getvalue()


def test_pipeline_persists_cells_and_continues_the_same_track(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    archive = ArchiveStore(tmp_path)
    first_time = datetime(2026, 8, 25, 10, 0, tzinfo=TIMEZONE)
    first_frame = _frame(first_time)
    second_frame = _frame(first_time + timedelta(minutes=4))
    archive.archive_frame(first_frame, _core_png(300, 300), first_time)
    archive.archive_frame(second_frame, _core_png(305, 302), second_frame.observed_at)
    pipeline = StormAnalysisPipeline(settings, archive)

    first_result = pipeline.process([first_frame, second_frame])
    state = archive.read_tracker_state()
    repeat_result = pipeline.process([first_frame, second_frame])

    assert first_result.processed_frames == 2
    assert first_result.segmented_cells == 2
    assert first_result.active_tracks == 1
    assert first_result.errors == ()
    assert state is not None
    assert state["tracks"][0]["point_count"] == 2
    assert state["tracks"][0]["motion_confidence"] == "medium"
    assert repeat_result.processed_frames == 0
    assert repeat_result.active_tracks == 1
    assert (archive.cells_dir / f"{first_frame.filename}.json").is_file()
