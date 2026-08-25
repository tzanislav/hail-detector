from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .archive import ArchiveStore
from .config import Settings
from .opera import OperaClient, OperaIngestionService


STATIC_DIR = Path(__file__).parent / "static"


def _safe_filename(filename: str) -> str:
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="resource not found")
    return filename


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    archive = ArchiveStore(settings.archive_root)
    app = FastAPI(
        title="Hail detector",
        version="0.1.0",
        description=(
            "Yarlovo CAPPI decision processing, OPERA comparison data, "
            "radar-signature assessment and debug dashboard."
        ),
    )
    opera_fetch_lock = Lock()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "dashboard.html")

    @app.get("/status")
    def status() -> dict[str, object]:
        yarlovo = archive.read_status() or {
            "state": "starting",
            "detail": "No poll result has been recorded yet.",
        }
        opera = archive.read_opera_status() or {
            "state": "starting" if settings.opera_enabled else "disabled",
            "detail": "No OPERA comparison poll has been recorded yet.",
        }
        current = dict(opera)
        current["primary_source"] = "eumetnet_opera_dbzh_maximum_reflectivity"
        current["yarlovo"] = yarlovo
        # Kept for a short transition period for API/dashboard consumers that
        # previously read the comparison feed from this nested key.
        current["opera"] = opera
        return current

    @app.get("/health")
    def health() -> JSONResponse:
        current = status()
        code = 200 if current.get("state") == "ready" else 503
        return JSONResponse(status_code=code, content=current)

    @app.get("/tracks")
    def tracks() -> dict[str, object]:
        return archive.read_opera_tracker_state() or {
            "source": "eumetnet_opera_dbzh_maximum_reflectivity",
            "last_processed_at": None,
            "next_track_number": 1,
            "tracks": [],
        }

    @app.get("/yarlovo/tracks")
    def yarlovo_tracks() -> dict[str, object]:
        return archive.read_tracker_state() or {
            "source": "yarlovo_cappi",
            "last_processed_at": None,
            "next_track_number": 1,
            "tracks": [],
        }

    @app.get("/assessments")
    def assessments() -> dict[str, object]:
        return {
            "target": settings.target.as_dict(),
            "assessments": [],
            "state": "disabled",
            "reason": "OPERA hail thresholds require separate validation before assessment.",
        }

    @app.get("/alerts")
    def alerts() -> dict[str, object]:
        return {
            "records": [],
            "state": "disabled",
            "reason": "Automatic alerts are disabled while OPERA hail calibration is pending.",
        }

    @app.get("/opera/status")
    def opera_status() -> dict[str, object]:
        return archive.read_opera_status() or {
            "state": "starting" if settings.opera_enabled else "disabled",
            "detail": "No OPERA comparison poll has been recorded yet.",
        }

    @app.get("/opera/history")
    def opera_history(hours: int = 24) -> dict[str, object]:
        if not 1 <= hours <= 24:
            raise HTTPException(status_code=422, detail="hours must be between 1 and 24")
        now = datetime.now(timezone.utc)
        frames = archive.list_opera_history(since=now - timedelta(hours=hours))
        return {
            "source": "eumetnet_opera_dbzh_maximum_reflectivity",
            "hours": hours,
            "available_frames": len(frames),
            "frames": frames,
        }

    @app.post("/opera/fetch")
    async def fetch_opera() -> dict[str, object]:
        """Request and archive the newest OPERA composites immediately."""

        if not settings.opera_enabled:
            raise HTTPException(status_code=409, detail="OPERA ingestion is disabled")
        if not opera_fetch_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="an OPERA fetch is already in progress")
        try:
            def poll_once() -> dict[str, object]:
                with OperaClient(settings.opera_api_url, settings.opera_lookback) as client:
                    return OperaIngestionService(settings, archive, client).poll_once().as_dict()

            return await run_in_threadpool(poll_once)
        finally:
            opera_fetch_lock.release()

    @app.get("/frames/{filename}", include_in_schema=False)
    def frame(filename: str) -> FileResponse:
        path = archive.frames_dir / _safe_filename(filename)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="radar frame not found")
        return FileResponse(path, media_type="image/png")

    @app.get("/cells/{filename}")
    def cells(filename: str) -> dict[str, object]:
        path = archive.cells_dir / f"{_safe_filename(filename)}.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="cell analysis not found")
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/opera/cells/{filename}")
    def opera_cells(filename: str) -> dict[str, object]:
        import json

        path = archive.opera_cells_dir / f"{_safe_filename(filename)}.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="OPERA cell analysis not found")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/opera/overlay/{filename}", include_in_schema=False)
    def opera_overlay(filename: str) -> FileResponse:
        frame = _safe_filename(filename)
        path = archive.opera_overlays_dir / f"{frame}.png"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="OPERA radar overlay not found")
        return FileResponse(path, media_type="image/png")

    return app


app = create_app()
