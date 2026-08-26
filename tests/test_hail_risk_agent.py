from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path

import h5py
import httpx
import pytest

from hail_detector.archive import ArchiveStore
from hail_detector.config import HailRiskAgentSettings, Settings, TargetLocation, TelegramSettings
from hail_detector.hail_policy import HailAssessment, SEVERE_ALERT_LEVEL, apply_llm_hail_risk
from hail_detector.hail_risk_agent import HailRiskAgent, HailRiskResearch, HailRiskResearchError
from hail_detector.opera import OperaAnalysisResult, OperaClient, OperaIngestionService
from hail_detector.telegram import TelegramNotifier


NOW = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
OPERA_FILE = "OPERA@20260826T0955@0@DBZH.h5"
OPERA_URL = f"https://files.example.test/{OPERA_FILE}"


def _assessment() -> dict[str, object]:
    return {
        "track_id": "track-000001",
        "max_dbz_band": [65, 70],
        "projected_target_intersection": True,
        "minutes_to_arrival": 20.0,
        "storm_context": {
            "latest_observation": {
                "observed_at": "2026-08-26T10:00:00+00:00",
                "latitude": 42.7,
                "longitude": 23.2,
            }
        },
    }


def _settings(root: Path) -> Settings:
    return Settings(
        archive_root=root,
        poll_interval_seconds=240,
        tracker_maximum_gap=timedelta(minutes=12),
        tracker_maximum_speed_kmh=160,
        opera_lookback=timedelta(minutes=70),
        opera_maximum_frame_age=timedelta(minutes=15),
    )


def _qualifying_hail_assessment() -> HailAssessment:
    return HailAssessment(
        source="eumetnet_opera_dbzh_maximum_reflectivity",
        track_id="track-000001",
        radar_signature="severe_reflectivity_signature",
        confidence="high",
        candidate_level=SEVERE_ALERT_LEVEL,
        max_dbz_band=(65, 70),
        projected_target_intersection=True,
        minutes_to_arrival=20,
        reasons=("deterministic candidate",),
    )


def _coverage() -> dict[str, object]:
    return {"type": "Coverage", "links": [{"rel": "items", "type": "application/x-odim", "href": OPERA_URL}]}


def _opera_hdf5() -> bytes:
    encoded = BytesIO()
    with h5py.File(encoded, "w") as file:
        root_what = file.create_group("what")
        root_what.attrs["date"] = b"20260826"
        root_what.attrs["time"] = b"095500"
        root_what.attrs["object"] = b"COMP"
        where = file.create_group("where")
        where.attrs["projdef"] = b"+proj=laea +lat_0=55 +lon_0=10 +x_0=1950000 +y_0=-2100000 +units=m +ellps=WGS84"
        where.attrs["xscale"] = 1000.0
        where.attrs["yscale"] = 1000.0
        where.attrs["xsize"] = 10
        where.attrs["ysize"] = 10
        dataset = file.create_group("dataset1")
        dataset.create_group("what").attrs["product"] = b"MAX"
        data = dataset.create_group("data1")
        data_what = data.create_group("what")
        data_what.attrs["quantity"] = b"DBZH"
        data_what.attrs["gain"] = 1.0
        data_what.attrs["offset"] = 0.0
        data_what.attrs["nodata"] = -9999.0
        data_what.attrs["undetect"] = -8888.0
        data.create_dataset("data", data=[[64.0] * 10 for _ in range(10)])
    return encoded.getvalue()


def test_agent_uses_web_search_and_returns_validated_structured_risk() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_123",
                "status": "completed",
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "sources": [{"title": "Official warning", "url": "https://meteo.example.test/warnings"}]
                        },
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "rating": 8,
                                        "description": "Hail is plausible near the protected region; this is not an official warning.",
                                        "sources": [{"title": "Forecast", "url": "https://forecast.example.test/"}],
                                    }
                                ),
                            }
                        ],
                    },
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    agent = HailRiskAgent(
        HailRiskAgentSettings(enabled=True, api_key="test-key", model="test-model"),
        client,
    )
    result = agent.assess(_assessment(), target=TargetLocation(), now=NOW)

    assert result.rating == 8
    assert result.response_id == "resp_123"
    assert len(result.sources) == 2
    assert requests[0].headers["authorization"] == "Bearer test-key"
    payload = json.loads(requests[0].content)
    assert payload["tools"] == [{"type": "web_search_preview"}]
    assert payload["tool_choice"] == "required"
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["text"]["format"]["type"] == "json_schema"
    assert '"longitude":23.3219' in payload["input"]


def test_research_rating_must_exceed_the_configured_alert_threshold() -> None:
    base = HailAssessment(
        source="eumetnet_opera_dbzh_maximum_reflectivity",
        track_id="track-000001",
        radar_signature="severe_reflectivity_signature",
        confidence="high",
        candidate_level=SEVERE_ALERT_LEVEL,
        max_dbz_band=(65, 70),
        projected_target_intersection=True,
        minutes_to_arrival=20,
        reasons=("radar candidate",),
    )
    at_threshold = apply_llm_hail_risk(
        base, {"rating": 7, "description": "Assessment at threshold."}, alert_threshold=7
    )
    above_threshold = apply_llm_hail_risk(
        base, {"rating": 8, "description": "Elevated hail concern."}, alert_threshold=7
    )

    assert at_threshold.candidate_level is None
    assert above_threshold.candidate_level == SEVERE_ALERT_LEVEL
    assert above_threshold.alert_description == "Elevated hail concern."


def test_automatic_research_is_reused_for_the_track_cooldown(tmp_path: Path) -> None:
    class ResearchAgent:
        def __init__(self) -> None:
            self.calls = 0

        def is_candidate(self, assessment: dict[str, object]) -> bool:
            return True

        def assess(self, *_args, now: datetime, **_kwargs) -> HailRiskResearch:
            self.calls += 1
            return HailRiskResearch(
                rating=8,
                description="Cached research still indicates elevated hail concern.",
                sources=(),
                model="test-model",
                response_id=f"resp_{self.calls}",
                researched_at=now,
            )

    settings = replace(
        _settings(tmp_path),
        hail_risk_agent=HailRiskAgentSettings(enabled=True, api_key="test-key"),
    )
    archive = ArchiveStore(tmp_path)
    agent = ResearchAgent()
    service = OperaIngestionService(
        settings,
        archive,
        None,  # type: ignore[arg-type]
        hail_risk_agent=agent,  # type: ignore[arg-type]
    )

    first, candidates, requests, successes, failures, cooldown_skips, errors = (
        service._research_assessments((_qualifying_hail_assessment(),), fetched_at=NOW)
    )
    cached, _, cached_requests, cached_successes, cached_failures, cached_skips, cached_errors = (
        service._research_assessments(
            (_qualifying_hail_assessment(),), fetched_at=NOW + timedelta(minutes=4)
        )
    )
    retried, _, retried_requests, retried_successes, retried_failures, retried_skips, retried_errors = (
        service._research_assessments(
            (_qualifying_hail_assessment(),), fetched_at=NOW + timedelta(minutes=30)
        )
    )

    assert candidates == 1
    assert requests == 1
    assert successes == 1
    assert failures == 0
    assert cooldown_skips == 0
    assert errors == ()
    assert first[0].llm_hail_risk is not None
    assert agent.calls == 2
    assert cached_requests == 0
    assert cached_successes == 0
    assert cached_failures == 0
    assert cached_skips == 1
    assert cached_errors == ()
    assert cached[0].llm_hail_risk is not None
    assert cached[0].llm_hail_risk["response_id"] == "resp_1"
    assert retried_requests == 1
    assert retried_successes == 1
    assert retried_failures == 0
    assert retried_skips == 0
    assert retried_errors == ()
    assert retried[0].llm_hail_risk is not None


def test_failed_automatic_research_starts_the_track_cooldown(tmp_path: Path) -> None:
    class FailedResearchAgent:
        def __init__(self) -> None:
            self.calls = 0

        def is_candidate(self, assessment: dict[str, object]) -> bool:
            return True

        def assess(self, *_args, **_kwargs) -> HailRiskResearch:
            self.calls += 1
            raise HailRiskResearchError("OpenAI research request failed")

    settings = replace(
        _settings(tmp_path),
        hail_risk_agent=HailRiskAgentSettings(enabled=True, api_key="test-key"),
    )
    archive = ArchiveStore(tmp_path)
    agent = FailedResearchAgent()
    service = OperaIngestionService(
        settings,
        archive,
        None,  # type: ignore[arg-type]
        hail_risk_agent=agent,  # type: ignore[arg-type]
    )

    _, _, first_requests, _, first_failures, _, _ = service._research_assessments(
        (_qualifying_hail_assessment(),), fetched_at=NOW
    )
    cooled, _, cooled_requests, _, cooled_failures, cooled_skips, cooled_errors = (
        service._research_assessments(
            (_qualifying_hail_assessment(),), fetched_at=NOW + timedelta(minutes=4)
        )
    )

    assert agent.calls == 1
    assert first_requests == 1
    assert first_failures == 1
    assert cooled_requests == 0
    assert cooled_failures == 0
    assert cooled_skips == 1
    assert cooled_errors == ()
    assert cooled[0].candidate_level is None


def test_agent_reports_when_reasoning_exhausts_the_output_budget() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        )

    agent = HailRiskAgent(
        HailRiskAgentSettings(enabled=True, api_key="test-key"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(HailRiskResearchError, match="increase HAIL_LLM_MAX_OUTPUT_TOKENS"):
        agent.assess(_assessment(), target=TargetLocation(), now=NOW)


def test_high_researched_risk_delivers_the_description_through_telegram(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []

    class Analysis:
        def process(self, _frames):
            assessment = HailAssessment(
                source="eumetnet_opera_dbzh_maximum_reflectivity",
                track_id="track-000001",
                radar_signature="severe_reflectivity_signature",
                confidence="high",
                candidate_level=SEVERE_ALERT_LEVEL,
                max_dbz_band=(65, 70),
                projected_target_intersection=True,
                minutes_to_arrival=20,
                reasons=("deterministic candidate",),
                storm_context={"latest_observation": {"latitude": 42.7, "longitude": 23.2}},
            )
            return OperaAnalysisResult(
                processed_frames=1,
                segmented_cells=1,
                qualified_storm_cells=1,
                active_tracks=1,
                forecasted_tracks=1,
                intersecting_target_tracks=1,
                earliest_intersection_at=NOW.isoformat(),
                assessments=(assessment,),
                errors=(),
            )

    class ResearchAgent:
        def is_candidate(self, assessment: dict[str, object]) -> bool:
            return True

        def assess(self, *_args, **_kwargs) -> HailRiskResearch:
            return HailRiskResearch(
                rating=8,
                description="Research finds conditions supportive of hail near Sofia; this is not an official warning.",
                sources=(),
                model="test-model",
                response_id="resp_test",
                researched_at=NOW,
            )

    def opera_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.example.test":
            return httpx.Response(200, json=_coverage())
        if str(request.url) == OPERA_URL:
            return httpx.Response(200, content=_opera_hdf5())
        return httpx.Response(404)

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    settings = replace(
        _settings(tmp_path),
        hail_risk_agent=HailRiskAgentSettings(enabled=True, api_key="test-key"),
    )
    archive = ArchiveStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(opera_handler)) as opera_http:
        with httpx.Client(transport=httpx.MockTransport(telegram_handler)) as telegram_http:
            notifier = TelegramNotifier(
                TelegramSettings(enabled=True, bot_token="token", chat_id="123"), telegram_http
            )
            result = OperaIngestionService(
                settings,
                archive,
                OperaClient("https://api.example.test/opera", settings.opera_lookback, opera_http),
                analysis=Analysis(),
                telegram_notifier=notifier,
                hail_risk_agent=ResearchAgent(),  # type: ignore[arg-type]
            ).poll_once(NOW)

    assert result.llm_research_successes == 1
    assert result.telegram_deliveries == 1
    assert "Research finds conditions supportive of hail" in str(sent[0]["text"])
    stored = archive.read_assessments()
    assert stored is not None
    assert stored["assessments"][0]["llm_hail_risk"]["rating"] == 8


def test_failed_research_does_not_mutate_or_deliver_alert_state(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []

    class Analysis:
        def process(self, _frames):
            assessment = HailAssessment(
                source="eumetnet_opera_dbzh_maximum_reflectivity",
                track_id="track-000001",
                radar_signature="severe_reflectivity_signature",
                confidence="high",
                candidate_level=SEVERE_ALERT_LEVEL,
                max_dbz_band=(65, 70),
                projected_target_intersection=True,
                minutes_to_arrival=20,
                reasons=("deterministic candidate",),
            )
            return OperaAnalysisResult(1, 1, 1, 1, 1, 1, NOW.isoformat(), (assessment,), ())

    class FailedResearchAgent:
        def is_candidate(self, assessment: dict[str, object]) -> bool:
            return True

        def assess(self, *_args, **_kwargs) -> HailRiskResearch:
            raise HailRiskResearchError("OpenAI research request failed")

    def opera_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.example.test":
            return httpx.Response(200, json=_coverage())
        if str(request.url) == OPERA_URL:
            return httpx.Response(200, content=_opera_hdf5())
        return httpx.Response(404)

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    settings = replace(
        _settings(tmp_path),
        hail_risk_agent=HailRiskAgentSettings(enabled=True, api_key="test-key"),
    )
    archive = ArchiveStore(tmp_path)
    archive.write_alert_state({"marker": "must-remain", "records": []})
    archive.write_notification_outbox(
        {
            "event_id": "pending-event",
            "kind": "activated",
            "track_id": "track-older",
            "level": SEVERE_ALERT_LEVEL,
            "source": "test",
            "previous_level": None,
            "occurred_at": NOW.isoformat(),
            "reason": "must not deliver while research is unavailable",
        }
    )
    with httpx.Client(transport=httpx.MockTransport(opera_handler)) as opera_http:
        with httpx.Client(transport=httpx.MockTransport(telegram_handler)) as telegram_http:
            result = OperaIngestionService(
                settings,
                archive,
                OperaClient("https://api.example.test/opera", settings.opera_lookback, opera_http),
                analysis=Analysis(),
                telegram_notifier=TelegramNotifier(
                    TelegramSettings(enabled=True, bot_token="token", chat_id="123"), telegram_http
                ),
                hail_risk_agent=FailedResearchAgent(),  # type: ignore[arg-type]
            ).poll_once(NOW)

    assert result.llm_research_failures == 1
    assert result.alert_processing_deferred is True
    assert archive.read_alert_state() == {"marker": "must-remain", "records": []}
    assert sent == []
