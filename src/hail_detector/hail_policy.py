from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping

from .config import SevereAlertSettings
from .tracking import StormTrack
from .trajectory import TrajectoryForecast


TRAJECTORY_CONFIDENCE_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
SEVERE_ALERT_LEVEL = "Severe Reflectivity Alert"


@dataclass(frozen=True)
class HailAssessment:
    """Auditable OPERA severe-reflectivity assessment, not an official warning."""

    source: str
    track_id: str
    radar_signature: str
    confidence: str
    candidate_level: str | None
    max_dbz_band: tuple[int, int]
    projected_target_intersection: bool
    minutes_to_arrival: float | None
    reasons: tuple[str, ...]
    storm_context: dict[str, object] | None = None
    llm_hail_risk: dict[str, object] | None = None
    alert_description: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def assess_track(
    track: StormTrack,
    forecast: TrajectoryForecast,
    policy: SevereAlertSettings,
) -> HailAssessment:
    """Assess a confirmed OPERA track against the configured severe rule."""

    latest = track.latest
    maximum_dbz = latest.max_dbz_lower
    minutes_to_arrival: float | None = None
    if forecast.intersects_target_radius and forecast.arrival_window_start is not None:
        minutes_to_arrival = (
            forecast.arrival_window_start - latest.observed_at
        ).total_seconds() / 60

    reasons = [
        "source: OPERA DBZH maximum-reflectivity composite",
        (
            "observed maximum-reflectivity band: "
            f"{latest.max_dbz_lower}-{latest.max_dbz_upper} dBZ"
        ),
    ]
    if minutes_to_arrival is None:
        reasons.append("candidate suppressed: no projected target-radius intersection")
    else:
        reasons.append(
            "the fitted track enters the configured target radius in "
            f"{minutes_to_arrival:.0f} minutes"
        )

    candidate_level: str | None = None
    if maximum_dbz < policy.minimum_dbz:
        reasons.append(
            f"candidate suppressed: below the {policy.minimum_dbz}+ dBZ severe threshold"
        )
        signature = "below_severe_threshold"
    elif minutes_to_arrival is None:
        signature = "severe_reflectivity_signature"
    elif (
        TRAJECTORY_CONFIDENCE_RANK.get(forecast.confidence, 0)
        < TRAJECTORY_CONFIDENCE_RANK[policy.minimum_trajectory_confidence]
    ):
        signature = "severe_reflectivity_signature"
        reasons.append(
            "candidate suppressed: trajectory confidence "
            f"{forecast.confidence!r} is below the required "
            f"{policy.minimum_trajectory_confidence!r}"
        )
    elif minutes_to_arrival > policy.maximum_lead_minutes:
        signature = "severe_reflectivity_signature"
        reasons.append(
            "candidate suppressed: target entry is outside the "
            f"{policy.maximum_lead_minutes}-minute alert window"
        )
    else:
        signature = "severe_reflectivity_signature"
        candidate_level = SEVERE_ALERT_LEVEL
        reasons.append(
            "meets the OPERA severe-reflectivity rule: "
            f"{policy.minimum_dbz}+ dBZ with target entry within "
            f"{policy.maximum_lead_minutes} minutes"
        )

    storm_context = {
        "latest_observation": {
            "observed_at": latest.observed_at.isoformat(),
            "latitude": latest.centroid_location.latitude,
            "longitude": latest.centroid_location.longitude,
            "area_square_km": latest.area_square_km,
            "area_at_or_above_square_km": latest.area_at_or_above_square_km,
            "max_dbz_band": [latest.max_dbz_lower, latest.max_dbz_upper],
        },
        "observations": [
            {
                "observed_at": point.observed_at.isoformat(),
                "latitude": point.centroid_location.latitude,
                "longitude": point.centroid_location.longitude,
                "area_square_km": point.area_square_km,
                "area_at_or_above_square_km": point.area_at_or_above_square_km,
                "max_dbz_band": [point.max_dbz_lower, point.max_dbz_upper],
            }
            for point in track.points[-5:]
        ],
        "trajectory": forecast.as_dict(),
    }
    return HailAssessment(
        source="eumetnet_opera_dbzh_maximum_reflectivity",
        track_id=track.track_id,
        radar_signature=signature,
        confidence=forecast.confidence,
        candidate_level=candidate_level,
        max_dbz_band=(latest.max_dbz_lower, latest.max_dbz_upper),
        projected_target_intersection=forecast.intersects_target_radius,
        minutes_to_arrival=minutes_to_arrival,
        reasons=tuple(reasons),
        storm_context=storm_context,
    )


def apply_llm_hail_risk(
    assessment: HailAssessment,
    research: Mapping[str, Any],
    *,
    alert_threshold: int,
) -> HailAssessment:
    """Make a web-researched rating the alert gate for an eligible radar track."""

    rating = research.get("rating")
    description = research.get("description")
    if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 10:
        raise ValueError("research rating must be an integer between 0 and 10")
    if not isinstance(description, str) or not (description := " ".join(description.split())):
        raise ValueError("research description must not be empty")
    if not 0 <= alert_threshold <= 10:
        raise ValueError("alert threshold must be between 0 and 10")

    qualifies = rating > alert_threshold
    outcome = (
        f"internet-researched hail-risk rating: {rating}/10 exceeds the alert threshold "
        f"> {alert_threshold}"
        if qualifies
        else f"candidate suppressed: internet-researched hail-risk rating {rating}/10 "
        f"does not exceed the alert threshold > {alert_threshold}"
    )
    return replace(
        assessment,
        candidate_level=SEVERE_ALERT_LEVEL if qualifies else None,
        radar_signature="web_researched_hail_risk",
        reasons=(*assessment.reasons, outcome),
        llm_hail_risk=dict(research),
        alert_description=description if qualifies else None,
    )


def suppress_for_unavailable_llm_research(
    assessment: HailAssessment,
    message: str,
) -> HailAssessment:
    """Fail closed when the enabled research gate cannot produce an assessment."""

    return replace(
        assessment,
        candidate_level=None,
        reasons=(*assessment.reasons, f"candidate suppressed: web research unavailable ({message})"),
    )
