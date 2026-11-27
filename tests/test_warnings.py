from datetime import datetime, timezone

import httpx

from hail_detector.archive import ArchiveStore
from hail_detector.warnings import WarningsClient, WarningsIngestionService


NOW = datetime(2026, 9, 11, 15, tzinfo=timezone.utc)
WARNING_URL = "https://api.example.test/warnings/collections/warnings/locations/BG"
DETAIL_URL = "https://details.example.test/warnings/example.json?temporary=signature"


def _feature() -> dict[str, object]:
    return {
        "properties": {"alertId": "alert-001"},
        "links": [{"rel": "json", "href": DETAIL_URL}],
    }


def _detail() -> dict[str, object]:
    return {
        "sent": "2026-09-11T06:00:00+00:00",
        "msgType": "Alert",
        "status": "Actual",
        "info": [
            {
                "language": "en",
                "event": "Thunderstorm",
                "severity": "Severe",
                "certainty": "Likely",
                "urgency": "Immediate",
                "effective": "2026-09-11T14:00:00+00:00",
                "expires": "2026-09-11T20:00:00+00:00",
                "headline": "Severe thunderstorm warning",
                "area": [{"areaDesc": "Sofia-city"}, {"areaDesc": "Sofia-city"}],
            }
        ],
    }


def test_warning_service_persists_safe_bulgaria_warning_summary(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.example.test":
            assert request.headers["apikey"] == "test-opera-key"
            assert request.url.params["page"] == "1"
            assert request.url.params["f"] == "json"
            assert request.url.params["datetime"] == "2026-09-10T15:01:00Z/2026-09-11T15:00:00Z"
            assert request.url.params["active"] == "2026-09-11T15:00:00Z/2026-09-12T15:00:00Z"
            return httpx.Response(200, json={"type": "FeatureCollection", "features": [_feature()]})
        if request.url.host == "details.example.test":
            assert "apikey" not in request.headers
            return httpx.Response(200, json=_detail())
        return httpx.Response(404)

    archive = ArchiveStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = WarningsIngestionService(
            archive,
            WarningsClient(WARNING_URL, "test-opera-key", http_client),
        ).poll_once(NOW)

    assert result.state == "ready"
    assert result.warnings == (
        {
            "alert_id": "alert-001",
            "sent_at": "2026-09-11T06:00:00+00:00",
            "message_type": "Alert",
            "status": "Actual",
            "language": "en",
            "event": "Thunderstorm",
            "severity": "Severe",
            "certainty": "Likely",
            "urgency": "Immediate",
            "effective": "2026-09-11T14:00:00+00:00",
            "onset": None,
            "expires": "2026-09-11T20:00:00+00:00",
            "headline": "Severe thunderstorm warning",
            "description": None,
            "instruction": None,
            "areas": ["Sofia-city"],
        },
    )
    status = archive.read_warnings_status()
    assert status is not None
    assert status["warning_count"] == 1
    assert DETAIL_URL not in str(status)


def test_warning_service_deduplicates_equivalent_cap_revisions(tmp_path) -> None:
    first = _feature()
    second = _feature()
    second["properties"] = {"alertId": "alert-002"}
    second["links"] = [{"rel": "json", "href": "https://details.example.test/warnings/second.json"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.example.test":
            return httpx.Response(200, json={"type": "FeatureCollection", "features": [first, second]})
        if request.url.host == "details.example.test":
            return httpx.Response(200, json=_detail())
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        warnings = WarningsClient(WARNING_URL, client=http_client).fetch_bulgaria_warnings(NOW)

    assert len(warnings) == 1
