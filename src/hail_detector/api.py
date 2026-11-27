from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import time
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .archive import ArchiveStore
from .config import Settings
from .hail_risk_agent import HailRiskAgent, HailRiskResearchError
from .opera import OperaClient, OperaIngestionService
from .opera_volume import OperaVolumeClient, OperaVolumeIngestionService
from .telegram import TelegramDeliveryError, TelegramNotifier
from .warnings import WarningsClient, WarningsIngestionService


STATIC_DIR = Path(__file__).parent / "static"
TELEGRAM_TEST_MINIMUM_INTERVAL_SECONDS = 3.0


class TelegramTestRequest(BaseModel):
    password: str = Field(default="", max_length=200)


def _safe_filename(filename: str) -> str:
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="resource not found")
    return filename


def _telegram_alert_status(settings: Settings, current: dict[str, object]) -> dict[str, object]:
    """Return a safe, user-facing summary of Telegram alert readiness."""

    criteria = {
        "minimum_dbz": settings.severe_alert.minimum_dbz,
        "maximum_lead_minutes": settings.severe_alert.maximum_lead_minutes,
        "minimum_trajectory_confidence": (
            settings.severe_alert.minimum_trajectory_confidence
        ),
    }
    if not settings.telegram.enabled:
        return {
            "configured": False,
            "active": False,
            "state": "disabled",
            "message": "Telegram alerts are turned off.",
            "criteria": criteria,
        }
    if bool(current.get("alert_processing_deferred", True)):
        return {
            "configured": True,
            "active": False,
            "state": "paused",
            "message": "Telegram is configured; alerts resume after a fresh OPERA cycle.",
            "criteria": criteria,
        }
    return {
        "configured": True,
        "active": True,
        "state": "active",
        "message": "Matching OPERA storms will send a Telegram alert.",
        "criteria": criteria,
    }


def _hail_risk_agent_status(settings: Settings, current: dict[str, object]) -> dict[str, object]:
    """Return configuration only; never expose the OpenAI API key."""

    agent = settings.hail_risk_agent
    criteria = {
        "candidate_minimum_dbz": agent.candidate_minimum_dbz,
        "maximum_lead_minutes": agent.maximum_lead_minutes,
        "alert_threshold": agent.alert_threshold,
        "research_cooldown_minutes": int(agent.research_cooldown.total_seconds() / 60),
    }
    if not agent.enabled:
        return {
            "configured": False,
            "active": False,
            "state": "disabled",
            "message": "Web-researched hail-risk assessments are turned off.",
            "criteria": criteria,
        }
    failures = current.get("llm_research_failures", 0)
    if isinstance(failures, int) and failures > 0:
        return {
            "configured": True,
            "active": False,
            "state": "degraded",
            "message": "The latest eligible storm could not receive a web-researched assessment; alerts were suppressed.",
            "criteria": criteria,
        }
    if bool(current.get("alert_processing_deferred", True)):
        return {
            "configured": True,
            "active": False,
            "state": "paused",
            "message": "Research runs after a fresh OPERA cycle identifies an eligible storm.",
            "criteria": criteria,
        }
    return {
        "configured": True,
        "active": True,
        "state": "active",
        "message": "Eligible target-bound storms are web-researched before alerting.",
        "criteria": criteria,
    }


def _matching_volume_profile(archive: ArchiveStore, track_id: str) -> dict[str, Any] | None:
    volume_status = archive.read_opera_volume_status()
    if not isinstance(volume_status, dict):
        return None
    profiles = volume_status.get("track_profiles")
    if not isinstance(profiles, list):
        return None
    for profile in profiles:
        if isinstance(profile, dict) and profile.get("track_id") == track_id:
            return profile
    return None


def _simulated_hail_risk_candidate(
    tracker_state: dict[str, Any],
    track_id: str,
    settings: Settings,
) -> dict[str, Any]:
    """Build a test-only qualifying candidate from an archived displayed track."""

    tracks = tracker_state.get("tracks")
    if not isinstance(tracks, list):
        raise HTTPException(status_code=404, detail="storm track not found")
    track = next(
        (
            item
            for item in tracks
            if isinstance(item, dict) and item.get("track_id") == track_id
        ),
        None,
    )
    if track is None:
        raise HTTPException(status_code=404, detail="storm track not found")
    points = track.get("points")
    if not isinstance(points, list) or not points:
        raise HTTPException(status_code=422, detail="storm track has no usable observations")
    minimum_confirmations = tracker_state.get("minimum_confirmed_observations", 1)
    if not isinstance(minimum_confirmations, int) or minimum_confirmations < 1:
        minimum_confirmations = 1
    if len(points) < minimum_confirmations:
        raise HTTPException(status_code=409, detail="storm track is not confirmed")
    latest = points[-1]
    if not isinstance(latest, dict):
        raise HTTPException(status_code=422, detail="storm track has no usable latest observation")
    lower = latest.get("max_dbz_lower")
    upper = latest.get("max_dbz_upper")
    if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)):
        raise HTTPException(status_code=422, detail="storm track has no usable dBZ band")

    simulated_lower = max(int(lower), settings.hail_risk_agent.candidate_minimum_dbz)
    simulated_upper = max(int(upper), simulated_lower + 5)
    maximum_lead = settings.hail_risk_agent.maximum_lead_minutes
    simulated_minutes_to_arrival = max(1, min(20, maximum_lead))
    forecasts = tracker_state.get("forecasts")
    forecast = next(
        (
            item
            for item in forecasts if isinstance(item, dict) and item.get("track_id") == track_id
        ),
        None,
    ) if isinstance(forecasts, list) else None
    return {
        "source": "eumetnet_opera_dbzh_maximum_reflectivity",
        "track_id": track_id,
        "radar_signature": "simulated_qualifying_hail_risk_test",
        "confidence": track.get("motion_confidence", "test"),
        "candidate_level": None,
        "max_dbz_band": [simulated_lower, simulated_upper],
        "projected_target_intersection": True,
        "minutes_to_arrival": simulated_minutes_to_arrival,
        "reasons": [
            "manual hail-risk agent test; simulated eligibility only",
            "this test does not create an alert or change alert state",
        ],
        "storm_context": {
            "test_mode": True,
            "actual_track": track,
            "actual_trajectory": forecast,
            "simulated_qualifying_criteria": {
                "projected_target_intersection": True,
                "max_dbz_band": [simulated_lower, simulated_upper],
                "minutes_to_arrival": simulated_minutes_to_arrival,
                "note": "Eligibility values are simulated only; archived source observations remain unchanged.",
            },
        },
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    archive = ArchiveStore(settings.archive_root)
    app = FastAPI(
        title="Hail detector",
        version="0.1.0",
        description="OPERA DBZH severe-reflectivity alerts and debug dashboard.",
    )
    opera_fetch_lock = Lock()
    hail_risk_test_lock = Lock()
    telegram_test_lock = Lock()
    telegram_test_last_attempt = [float("-inf")]
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
        current["polling"] = archive.read_opera_polling_state() or {
            "mode": "standard",
            "next_poll_interval_seconds": settings.poll_interval_seconds,
            "standard_poll_interval_seconds": settings.poll_interval_seconds,
        }
        current["telegram_alerts"] = _telegram_alert_status(settings, current)
        current["hail_risk_agent"] = _hail_risk_agent_status(settings, current)
        current["volume"] = archive.read_opera_volume_status() or {
            "state": "starting" if settings.opera_volume_enabled else "disabled",
            "experimental": True,
            "detail": "No Craiova volume poll has been recorded yet.",
        }
        current["warnings"] = archive.read_warnings_status() or {
            "source": "MeteoAlarm official warnings via MeteoGate",
            "country_code": "BG",
            "state": "starting" if settings.warnings_enabled else "disabled",
            "detail": "No Bulgaria warning poll has been recorded yet.",
            "warning_count": 0,
            "warnings": [],
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

    @app.get("/warnings/bulgaria")
    def bulgaria_warnings() -> dict[str, object]:
        return archive.read_warnings_status() or {
            "source": "MeteoAlarm official warnings via MeteoGate",
            "country_code": "BG",
            "state": "starting" if settings.warnings_enabled else "disabled",
            "detail": "No Bulgaria warning poll has been recorded yet.",
            "warning_count": 0,
            "warnings": [],
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
                hail_risk_agent = (
                    HailRiskAgent(settings.hail_risk_agent)
                    if settings.hail_risk_agent.enabled
                    else None
                )
                try:
                    with OperaClient(
                        settings.opera_api_url,
                        settings.opera_lookback,
                        api_key=settings.opera_api_key,
                    ) as client:
                        result = OperaIngestionService(
                            settings,
                            archive,
                            client,
                            telegram_notifier=notifier,
                            hail_risk_agent=hail_risk_agent,
                        ).poll_once().as_dict()
                    if settings.opera_volume_enabled:
                        with OperaVolumeClient(
                            settings.opera_volume_api_url,
                            settings.opera_volume_lookback,
                            api_key=settings.opera_api_key,
                        ) as volume_client:
                            volume = OperaVolumeIngestionService(
                                settings, archive, volume_client
                            ).poll_once()
                        result["volume"] = volume.as_dict(settings)
                    if settings.warnings_enabled:
                        with WarningsClient(
                            settings.warnings_api_url, settings.opera_api_key
                        ) as warnings_client:
                            warnings = WarningsIngestionService(
                                archive, warnings_client
                            ).poll_once()
                        result["warnings"] = warnings.as_dict()
                    return result
                finally:
                    if notifier is not None:
                        notifier.close()
                    if hail_risk_agent is not None:
                        hail_risk_agent.close()

            return await run_in_threadpool(poll_once)
        finally:
            opera_fetch_lock.release()

    @app.post("/telegram/test")
    async def test_telegram(request: TelegramTestRequest) -> dict[str, object]:
        """Send a password-protected test message through the configured bot."""

        if not settings.telegram.enabled:
            raise HTTPException(status_code=409, detail="Telegram is turned off on the server.")
        expected = settings.telegram.test_password
        if not expected:
            raise HTTPException(
                status_code=409,
                detail="Telegram testing is not set up: HAIL_TELEGRAM_TEST_PASSWORD is missing on the server.",
            )
        if not telegram_test_lock.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="A Telegram test is already in progress.")
        try:
            # Slow down password guessing: one attempt every few seconds.
            now = time.monotonic()
            if now - telegram_test_last_attempt[0] < TELEGRAM_TEST_MINIMUM_INTERVAL_SECONDS:
                raise HTTPException(status_code=429, detail="Please wait a few seconds and try again.")
            telegram_test_last_attempt[0] = now
            if not hmac.compare_digest(request.password.encode("utf-8"), expected.encode("utf-8")):
                raise HTTPException(status_code=401, detail="Incorrect password.")

            sent_at = datetime.now(timezone.utc)

            def send() -> None:
                notifier = TelegramNotifier(settings.telegram)
                try:
                    notifier.send_message(
                        "✅ Hail detector test message\n"
                        "Sent from the dashboard. Telegram delivery is working.\n"
                        "This is a test, not a weather alert."
                    )
                finally:
                    notifier.close()

            try:
                await run_in_threadpool(send)
            except TelegramDeliveryError as error:
                raise HTTPException(status_code=502, detail=f"Telegram did not accept the test: {error}") from error
            return {"sent": True, "sent_at": sent_at.isoformat()}
        finally:
            telegram_test_lock.release()

    @app.post("/hail-risk/test/{track_id}")
    async def test_hail_risk_agent(track_id: str) -> dict[str, object]:
        """Run a manual, non-alerting research request for one displayed track."""

        if not settings.hail_risk_agent.enabled:
            raise HTTPException(status_code=409, detail="web-researched hail-risk agent is disabled")
        tracker_state = archive.read_opera_tracker_state()
        if tracker_state is None:
            raise HTTPException(status_code=404, detail="no storm tracks are available")
        candidate = _simulated_hail_risk_candidate(tracker_state, track_id, settings)
        if not hail_risk_test_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a hail-risk agent test is already in progress")
        try:
            def run_test() -> dict[str, object]:
                agent = HailRiskAgent(settings.hail_risk_agent)
                try:
                    research = agent.assess(
                        candidate,
                        target=settings.target,
                        volume_profile=_matching_volume_profile(archive, track_id),
                        include_debug_details=True,
                    )
                    return {
                        "test_mode": True,
                        "track_id": track_id,
                        "simulated_candidate": candidate,
                        "hail_risk_assessment": research.as_dict(),
                    }
                finally:
                    agent.close()

            try:
                return await run_in_threadpool(run_test)
            except HailRiskResearchError as error:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": f"hail-risk agent test failed: {error}",
                        "openai_debug": error.debug_details(),
                    },
                ) from error
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail=f"hail-risk agent test could not be prepared: {error}",
                ) from error
        finally:
            hail_risk_test_lock.release()

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
