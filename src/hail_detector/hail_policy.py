from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import HailPolicySettings
from .tracking import StormTrack
from .trajectory import TrajectoryForecast


@dataclass(frozen=True)
class HailAssessment:
    """Auditable radar-signature assessment; never an official warning."""

    track_id: str
    radar_signature: str
    confidence: str
    score: int
    candidate_level: str | None
    calibration_gate_open: bool
    max_dbz_band: tuple[int, int]
    high_dbz_core_area_square_km: float
    projected_target_intersection: bool
    minutes_to_arrival: float | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _radar_signature(maximum_dbz: int, policy: HailPolicySettings) -> str:
    if maximum_dbz >= policy.severe_minimum_dbz:
        return "strong_severe_hail_signal"
    if maximum_dbz >= policy.warning_minimum_dbz:
        return "likely_hail_signature"
    if maximum_dbz >= policy.watch_minimum_dbz:
        return "possible_hail_signature"
    return "below_hail_threshold"


def _confidence(score: int, trajectory_confidence: str) -> str:
    if score >= 80 and trajectory_confidence in {"medium", "high"}:
        return "high"
    if score >= 55 and trajectory_confidence != "none":
        return "medium"
    if score > 0:
        return "low"
    return "none"


def assess_track(
    track: StormTrack,
    forecast: TrajectoryForecast,
    policy: HailPolicySettings,
) -> HailAssessment:
    """Score a track conservatively from observed radar bands and its projection."""

    latest = track.latest
    maximum_dbz = latest.max_dbz_lower
    signature = _radar_signature(maximum_dbz, policy)
    high_core_area = latest.area_at_or_above_square_km.get(
        f">={policy.watch_minimum_dbz}", 0.0
    )
    reasons: list[str] = [
        f"observed radar signature: {signature} ({latest.max_dbz_lower}-{latest.max_dbz_upper} dBZ band)"
    ]
    score = 0
    if signature == "possible_hail_signature":
        score += 35
    elif signature == "likely_hail_signature":
        score += 50
    elif signature == "strong_severe_hail_signal":
        score += 65
    else:
        reasons.append("observed reflectivity is below the conservative 55 dBZ hail threshold")

    if high_core_area >= 5:
        score += 15
        reasons.append(f"{high_core_area:.1f} km² of the cell meets the 55+ dBZ lower bound")
    elif high_core_area > 0:
        score += 7
        reasons.append(f"a small {high_core_area:.1f} km² 55+ dBZ core is present")

    if len(track.points) >= 2:
        intensity_change = latest.max_dbz_lower - track.points[-2].max_dbz_lower
        if intensity_change >= 5:
            score += 10
            reasons.append("the observed maximum reflectivity is strengthening")
        elif intensity_change <= -5:
            score -= 5
            reasons.append("the observed maximum reflectivity is weakening")

    minutes_to_arrival: float | None = None
    if forecast.intersects_target_radius and forecast.arrival_window_start is not None:
        minutes_to_arrival = (
            forecast.arrival_window_start - latest.observed_at
        ).total_seconds() / 60
        score += 15
        reasons.append(
            f"the current constant-velocity track intersects the configured target radius in {minutes_to_arrival:.0f} minutes"
        )
    elif forecast.available:
        reasons.append("the projected path does not intersect the configured target radius")
    else:
        reasons.append("trajectory is unavailable because motion evidence is insufficient")

    candidate_level: str | None = None
    if signature == "below_hail_threshold":
        pass
    elif not policy.high_dbz_calibration_validated:
        reasons.append(
            "candidate suppressed: high dBZ raw PNG colours have not been validated from a convective source frame"
        )
    elif minutes_to_arrival is None:
        reasons.append("candidate suppressed: no projected target-radius intersection")
    elif (
        maximum_dbz >= policy.warning_minimum_dbz
        and policy.warning_minimum_lead_minutes
        <= minutes_to_arrival
        <= policy.warning_maximum_lead_minutes
    ):
        candidate_level = "Hail Warning"
        reasons.append("meets the provisional probable-hail warning rule")
    elif (
        maximum_dbz >= policy.watch_minimum_dbz
        and policy.watch_minimum_lead_minutes
        <= minutes_to_arrival
        <= policy.watch_maximum_lead_minutes
    ):
        candidate_level = "Hail Watch"
        reasons.append("meets the provisional possible-hail watch rule")
    else:
        reasons.append("target intersection is outside the candidate lead-time windows")

    return HailAssessment(
        track_id=track.track_id,
        radar_signature=signature,
        confidence=_confidence(score, forecast.confidence),
        score=max(0, score),
        candidate_level=candidate_level,
        calibration_gate_open=policy.high_dbz_calibration_validated,
        max_dbz_band=(latest.max_dbz_lower, latest.max_dbz_upper),
        high_dbz_core_area_square_km=high_core_area,
        projected_target_intersection=forecast.intersects_target_radius,
        minutes_to_arrival=minutes_to_arrival,
        reasons=tuple(reasons),
    )
