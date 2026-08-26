from __future__ import annotations

from dataclasses import asdict, dataclass

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
    )
