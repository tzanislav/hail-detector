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
from .opera_volume import OperaVolumeClient, OperaVolumeIngestionService
from .telegram import TelegramNotifier


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
        description="OPERA DBZH severe-reflectivity alerts and debug dashboard.",
    )
    opera_fetch_lock = Lock()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "dashboard.html")

    @app.get("/status")
    def status() -> dict[str, object]:
        current = dict(
            archive.read_opera_status()
            or {
                "state": "starting" if settings.opera_enabled else "disabled",
                "detail": "No OPERA composite poll has been recorded yet.",
            }
        )
        current["primary_source"] = "eumetnet_opera_dbzh_maximum_reflectivity"
        current["volume"] = archive.read_opera_volume_status() or {
            "state": "starting" if settings.opera_volume_enabled else "disabled",
            "experimental": True,
            "detail": "No Craiova volume poll has been recorded yet.",
        }
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

    @app.get("/assessments")
    def assessments() -> dict[str, object]:
        return archive.read_assessments() or {
            "target": settings.target.as_dict(),
            "assessments": [],
        }

    @app.get("/alerts")
    def alerts() -> dict[str, object]:
        return archive.read_alert_state() or {"records": []}

    @app.get("/opera/volume/status")
    def opera_volume_status() -> dict[str, object]:
        return archive.read_opera_volume_status() or {
            "state": "starting" if settings.opera_volume_enabled else "disabled",
            "experimental": True,
            "detail": "No Craiova volume poll has been recorded yet.",
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
        if not settings.opera_enabled:
            raise HTTPException(status_code=409, detail="OPERA ingestion is disabled")
        if not opera_fetch_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="an OPERA fetch is already in progress")
        try:
            def poll_once() -> dict[str, object]:
                notifier = (
                    TelegramNotifier(settings.telegram)
                    if settings.telegram.enabled
                    else None
                )
                try:
                    with OperaClient(settings.opera_api_url, settings.opera_lookback) as client:
                        result = OperaIngestionService(
                            settings, archive, client, telegram_notifier=notifier
                        ).poll_once().as_dict()
                    if settings.opera_volume_enabled:
                        with OperaVolumeClient(
                            settings.opera_volume_api_url, settings.opera_volume_lookback
                        ) as volume_client:
                            volume = OperaVolumeIngestionService(
                                settings, archive, volume_client
                            ).poll_once()
                        result["volume"] = volume.as_dict(settings)
                    return result
                finally:
                    if notifier is not None:
                        notifier.close()

            return await run_in_threadpool(poll_once)
        finally:
            opera_fetch_lock.release()

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
