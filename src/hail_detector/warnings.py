"""MeteoAlarm warning ingestion for the Bulgaria dashboard panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from .archive import ArchiveStore

if TYPE_CHECKING:
    from .warning_alerts import SofiaWarningAlerter


LOGGER = logging.getLogger(__name__)


MAX_WARNING_PAGES = 5
MAX_WARNING_DETAILS = 25


class WarningsProtocolError(RuntimeError):
    """Raised when a MeteoGate warning response cannot be interpreted safely."""


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _json_detail_url(feature: dict[str, Any]) -> str | None:
    links = feature.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, dict) or link.get("rel") != "json":
            continue
        href = _text(link.get("href"))
        parsed = urlparse(href or "")
        if href and parsed.scheme == "https" and parsed.netloc:
            return href
    return None


def _warning_from_cap(payload: dict[str, Any], alert_id: str) -> dict[str, object]:
    infos = payload.get("info")
    if not isinstance(infos, list):
        raise WarningsProtocolError("official warning detail has no info list")
    candidates = [info for info in infos if isinstance(info, dict)]
    if not candidates:
        raise WarningsProtocolError("official warning detail has no usable info")

    # Bulgaria's CAP messages normally include a Bulgarian-language entry. Keep
    # that official local wording when it is available.
    info = next(
        (
            candidate
            for candidate in candidates
            if (_text(candidate.get("language")) or "").lower().startswith("bg")
        ),
        candidates[0],
    )
    areas: list[str] = []
    raw_areas = info.get("area")
    if isinstance(raw_areas, list):
        for area in raw_areas:
            if isinstance(area, dict):
                description = _text(area.get("areaDesc"))
                if description and description not in areas:
                    areas.append(description)

    return {
        "alert_id": alert_id,
        "sent_at": _text(payload.get("sent")),
        "message_type": _text(payload.get("msgType")),
        "status": _text(payload.get("status")),
        "language": _text(info.get("language")),
        "event": _text(info.get("event")) or "Official weather warning",
        "severity": _text(info.get("severity")) or "Unknown",
        "certainty": _text(info.get("certainty")),
        "urgency": _text(info.get("urgency")),
        "effective": _text(info.get("effective")),
        "onset": _text(info.get("onset")),
        "expires": _text(info.get("expires")),
        "headline": _text(info.get("headline")),
        "description": _text(info.get("description")),
        "instruction": _text(info.get("instruction")),
        "areas": areas,
    }


def _warning_identity(warning: dict[str, object]) -> tuple[object, ...]:
    areas = warning.get("areas")
    area_names = tuple(sorted(str(area) for area in areas)) if isinstance(areas, list) else ()
    return (
        warning.get("language"),
        warning.get("event"),
        warning.get("severity"),
        warning.get("effective"),
        warning.get("expires"),
        warning.get("headline"),
        area_names,
    )


@dataclass(frozen=True)
class WarningsPollResult:
    state: str
    fetched_at: datetime
    warnings: tuple[dict[str, object], ...]
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "source": "MeteoAlarm official warnings via MeteoGate",
            "country_code": "BG",
            "state": self.state,
            "fetched_at": self.fetched_at.isoformat(),
            "warning_count": len(self.warnings),
            "warnings": list(self.warnings),
            "error": self.error,
        }


class WarningsClient:
    """Retrieve official Bulgarian warning messages through MeteoGate."""

    def __init__(
        self,
        api_url: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.api_url = api_url
        self._headers = {"apikey": api_key} if api_key else {}
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "WarningsClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch_bulgaria_warnings(
        self, now: datetime | None = None
    ) -> tuple[dict[str, object], ...]:
        fetched_at = now or datetime.now(timezone.utc)
        if fetched_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        fetched_at = fetched_at.astimezone(timezone.utc)
        sent_from = fetched_at - timedelta(hours=23, minutes=59)
        active_until = fetched_at + timedelta(hours=24)
        base_params = {
            "datetime": f"{sent_from:%Y-%m-%dT%H:%M:%SZ}/{fetched_at:%Y-%m-%dT%H:%M:%SZ}",
            "active": f"{fetched_at:%Y-%m-%dT%H:%M:%SZ}/{active_until:%Y-%m-%dT%H:%M:%SZ}",
            "f": "json",
        }
        detail_urls: dict[str, str] = {}
        for page in range(1, MAX_WARNING_PAGES + 1):
            params = dict(base_params)
            params["page"] = str(page)
            response = self._client.get(self.api_url, headers=self._headers, params=params)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as error:
                raise WarningsProtocolError("warning response is not valid JSON") from error
            if not isinstance(payload, dict):
                raise WarningsProtocolError("warning response root must be a JSON object")
            features = payload.get("features")
            if not isinstance(features, list):
                raise WarningsProtocolError("warning response has no features list")
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                properties = feature.get("properties")
                if not isinstance(properties, dict):
                    continue
                alert_id = _text(properties.get("alertId"))
                detail_url = _json_detail_url(feature)
                if alert_id and detail_url:
                    detail_urls.setdefault(alert_id, detail_url)
            if len(features) < 100:
                break

        warnings: list[dict[str, object]] = []
        for alert_id, detail_url in list(detail_urls.items())[:MAX_WARNING_DETAILS]:
            response = self._client.get(detail_url)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise WarningsProtocolError(
                    "could not retrieve official warning detail"
                ) from error
            try:
                detail = response.json()
            except ValueError as error:
                raise WarningsProtocolError("official warning detail is not valid JSON") from error
            if not isinstance(detail, dict):
                raise WarningsProtocolError("official warning detail must be a JSON object")
            warnings.append(_warning_from_cap(detail, alert_id))

        distinct_warnings: dict[tuple[object, ...], dict[str, object]] = {}
        for warning in warnings:
            distinct_warnings.setdefault(_warning_identity(warning), warning)
        return tuple(
            sorted(
                distinct_warnings.values(),
                key=lambda warning: str(warning.get("expires") or ""),
            )
        )


class WarningsIngestionService:
    """Persist a safe, compact official-warning snapshot for the dashboard."""

    def __init__(
        self,
        archive: ArchiveStore,
        client: WarningsClient,
        sofia_alerter: "SofiaWarningAlerter | None" = None,
    ):
        self.archive = archive
        self.client = client
        self.sofia_alerter = sofia_alerter

    def poll_once(self, now: datetime | None = None) -> WarningsPollResult:
        fetched_at = now or datetime.now(timezone.utc)
        if fetched_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        fetched_at = fetched_at.astimezone(timezone.utc)
        try:
            warnings = self.client.fetch_bulgaria_warnings(fetched_at)
            result = WarningsPollResult("ready", fetched_at, warnings)
        except Exception as error:
            result = WarningsPollResult("error", fetched_at, (), str(error))
        self.archive.write_warnings_status(result.as_dict())
        if self.sofia_alerter is not None and result.state == "ready":
            try:
                self.sofia_alerter.process(result.warnings, fetched_at)
            except Exception:  # never let a notification problem break ingestion
                LOGGER.exception("Sofia warning Telegram processing failed")
        return result
