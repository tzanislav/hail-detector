from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from .config import HailRiskAgentSettings, TargetLocation


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


class HailRiskResearchError(RuntimeError):
    """A safe-to-log failure while requesting a web-researched assessment."""

    def __init__(
        self,
        message: str,
        *,
        request: Mapping[str, Any] | None = None,
        response: object = None,
    ):
        super().__init__(message)
        self.request = dict(request) if request is not None else None
        self.response = response

    def debug_details(self) -> dict[str, object]:
        """Return request/response diagnostics for the manual test route only."""

        details: dict[str, object] = {}
        if self.request is not None:
            details["request"] = self.request
        if self.response is not None:
            details["response"] = self.response
        return details


@dataclass(frozen=True)
class HailRiskResearch:
    """A structured, auditable web-researched risk opinion.

    The rating is an assistant assessment of hail risk, not an official warning
    and not a replacement for the primary radar observation.
    """

    rating: int
    description: str
    sources: tuple[dict[str, str], ...]
    model: str
    response_id: str | None
    researched_at: datetime
    debug_details: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["sources"] = list(self.sources)
        value["researched_at"] = self.researched_at.isoformat()
        if self.debug_details is None:
            value.pop("debug_details", None)
        return value


RISK_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "rating": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
            "description": "Estimated hail risk on a 0 to 10 scale.",
        },
        "description": {
            "type": "string",
            "maxLength": 1200,
            "description": (
                "A concise, practical assessment for the protected region. "
                "State uncertainty and that this is not an official warning."
            ),
        },
        "sources": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "maxLength": 200},
                    "url": {"type": "string", "maxLength": 2000},
                },
                "required": ["title", "url"],
            },
        },
    },
    "required": ["rating", "description", "sources"],
}


def _safe_source(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    url = value.get("url")
    if not isinstance(url, str) or urlparse(url).scheme not in {"http", "https"}:
        return None
    title = value.get("title")
    return {
        "title": " ".join(title.split())[:200] if isinstance(title, str) else url,
        "url": url[:2000],
    }


def _web_sources(response: Mapping[str, Any]) -> list[dict[str, str]]:
    """Collect included Responses web-search sources without trusting their shape."""

    sources: list[dict[str, str]] = []
    for item in response.get("output", []):
        if not isinstance(item, Mapping) or item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        if not isinstance(action, Mapping):
            continue
        for source in action.get("sources", []):
            safe = _safe_source(source)
            if safe is not None:
                sources.append(safe)
    return sources


def _unique_sources(sources: list[dict[str, str]]) -> tuple[dict[str, str], ...]:
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for source in sources:
        url = source["url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(source)
        if len(unique) == 5:
            break
    return tuple(unique)


def _output_text(response: Mapping[str, Any]) -> str | None:
    """Read final text from either an SDK convenience field or REST output items."""

    direct = response.get("output_text")
    if isinstance(direct, str):
        return direct
    output = response.get("output")
    if not isinstance(output, list):
        return None
    messages: list[str] = []
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, Mapping)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                messages.append(part["text"])
    return "\n".join(messages) if messages else None


class HailRiskAgent:
    """Request a bounded web-researched hail assessment from the Responses API."""

    def __init__(
        self,
        settings: HailRiskAgentSettings,
        client: httpx.Client | None = None,
    ):
        if not settings.enabled:
            raise ValueError("HailRiskAgent requires enabled HAIL_LLM settings")
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=settings.request_timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def is_candidate(self, assessment: Mapping[str, Any]) -> bool:
        """Limit internet calls to fresh-looking, potentially impactful tracks."""

        if assessment.get("projected_target_intersection") is not True:
            return False
        minutes_to_arrival = assessment.get("minutes_to_arrival")
        if not isinstance(minutes_to_arrival, (int, float)):
            return False
        if minutes_to_arrival < 0 or minutes_to_arrival > self.settings.maximum_lead_minutes:
            return False
        band = assessment.get("max_dbz_band")
        return (
            isinstance(band, (list, tuple))
            and bool(band)
            and isinstance(band[0], (int, float))
            and band[0] >= self.settings.candidate_minimum_dbz
        )

    def _input_for(
        self,
        assessment: Mapping[str, Any],
        target: TargetLocation,
        volume_profile: Mapping[str, Any] | None,
    ) -> str:
        context = {
            "protected_region": target.as_dict(),
            "radar_assessment": assessment,
            "experimental_volume_profile": volume_profile,
        }
        return (
            "Assess the short-term hail risk to the protected region using the "
            "radar-track context below. Use web search before deciding, prioritizing "
            "current official meteorological sources for the location and time. Treat "
            "the supplied OPERA DBZH observations as radar signatures, not confirmed "
            "hail. Do not invent current observations, do not issue an official warning, "
            "and make uncertainty explicit.\n\n"
            f"Radar context:\n{json.dumps(context, separators=(',', ':'), ensure_ascii=False)}"
        )

    def assess(
        self,
        assessment: Mapping[str, Any],
        *,
        target: TargetLocation,
        volume_profile: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        include_debug_details: bool = False,
    ) -> HailRiskResearch:
        if not self.is_candidate(assessment):
            raise ValueError("assessment is not eligible for web research")
        researched_at = now or datetime.now(timezone.utc)
        if researched_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        payload = {
            "model": self.settings.model,
            "reasoning": {"effort": self.settings.reasoning_effort},
            "store": False,
            "max_output_tokens": self.settings.max_output_tokens,
            "tools": [{"type": "web_search_preview"}],
            "tool_choice": "required",
            "max_tool_calls": 2,
            "include": ["web_search_call.action.sources"],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "hail_risk_assessment",
                    "strict": True,
                    "schema": RISK_RESPONSE_SCHEMA,
                }
            },
            "input": self._input_for(assessment, target, volume_profile),
        }
        try:
            response = self.client.post(
                OPENAI_RESPONSES_URL,
                headers={"Authorization": f"Bearer {self.settings.api_key}"},
                json=payload,
            )
        except httpx.HTTPError as error:
            raise HailRiskResearchError(
                "OpenAI research request failed",
                request=payload if include_debug_details else None,
            ) from error
        if response.status_code < 200 or response.status_code >= 300:
            try:
                rejected_response: object = response.json()
            except ValueError:
                rejected_response = response.text
            raise HailRiskResearchError(
                f"OpenAI research request was rejected (HTTP {response.status_code})",
                request=payload if include_debug_details else None,
                response=rejected_response if include_debug_details else None,
            )
        try:
            body = response.json()
        except ValueError as error:
            raise HailRiskResearchError(
                "OpenAI returned an invalid research response",
                request=payload if include_debug_details else None,
                response=response.text if include_debug_details else None,
            ) from error
        if not isinstance(body, Mapping):
            raise HailRiskResearchError(
                "OpenAI returned an invalid research response",
                request=payload if include_debug_details else None,
                response=body if include_debug_details else None,
            )
        output_text = _output_text(body)
        if output_text is None:
            status = body.get("status")
            incomplete_details = body.get("incomplete_details")
            incomplete_reason = (
                incomplete_details.get("reason")
                if isinstance(incomplete_details, Mapping)
                else None
            )
            if incomplete_reason == "max_output_tokens":
                raise HailRiskResearchError(
                    "OpenAI research response was incomplete because it reached "
                    "max_output_tokens; increase HAIL_LLM_MAX_OUTPUT_TOKENS",
                    request=payload if include_debug_details else None,
                    response=dict(body) if include_debug_details else None,
                )
            if isinstance(status, str):
                raise HailRiskResearchError(
                    f"OpenAI research response ended with status {status!r} without structured output",
                    request=payload if include_debug_details else None,
                    response=dict(body) if include_debug_details else None,
                )
            raise HailRiskResearchError(
                "OpenAI research response has no structured output",
                request=payload if include_debug_details else None,
                response=dict(body) if include_debug_details else None,
            )
        try:
            result = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise HailRiskResearchError(
                "OpenAI research output was not valid JSON",
                request=payload if include_debug_details else None,
                response=dict(body) if include_debug_details else None,
            ) from error
        if not isinstance(result, Mapping):
            raise HailRiskResearchError(
                "OpenAI research output was not an object",
                request=payload if include_debug_details else None,
                response=dict(body) if include_debug_details else None,
            )
        rating = result.get("rating")
        description = result.get("description")
        if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 10:
            raise HailRiskResearchError(
                "OpenAI research rating was outside 0 through 10",
                request=payload if include_debug_details else None,
                response=dict(body) if include_debug_details else None,
            )
        if not isinstance(description, str) or not (description := " ".join(description.split())):
            raise HailRiskResearchError(
                "OpenAI research description was empty",
                request=payload if include_debug_details else None,
                response=dict(body) if include_debug_details else None,
            )

        declared_sources = [
            source
            for source in (_safe_source(item) for item in result.get("sources", []))
            if source is not None
        ] if isinstance(result.get("sources"), list) else []
        sources = _unique_sources(_web_sources(body) + declared_sources)
        response_id = body.get("id")
        return HailRiskResearch(
            rating=rating,
            description=description[:1200],
            sources=sources,
            model=self.settings.model,
            response_id=response_id if isinstance(response_id, str) else None,
            researched_at=researched_at,
            debug_details=(
                {"request": payload, "response": dict(body)}
                if include_debug_details
                else None
            ),
        )
