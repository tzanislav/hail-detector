from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import os
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_OPERA_API_URL = (
    "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/"
    "observations/locations/0-20010-0-OPERA"
)
DEFAULT_OPERA_VOLUME_RADAR_ID = "0-642-0-rocra"
DEFAULT_OPERA_VOLUME_RADAR_NAME = "Craiova, Romania"
DEFAULT_OPERA_VOLUME_API_URL = (
    "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/"
    f"observations/locations/{DEFAULT_OPERA_VOLUME_RADAR_ID}"
)


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class TargetLocation:
    """A fixed protected point; defaults are development Sofia-centre values."""

    latitude: float = 42.6977
    longitude: float = 23.3219
    warning_radius_km: float = 10.0

    def __post_init__(self) -> None:
        if not -90 <= self.latitude <= 90:
            raise ValueError("target latitude must be between -90 and 90")
        if not -180 <= self.longitude <= 180:
            raise ValueError("target longitude must be between -180 and 180")
        if self.warning_radius_km <= 0:
            raise ValueError("target warning radius must be positive")

    def as_dict(self) -> dict[str, float]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "warning_radius_km": self.warning_radius_km,
        }


@dataclass(frozen=True)
class SevereAlertSettings:
    """OPERA DBZH alert criteria; these are not official weather warnings."""

    minimum_dbz: int = 65
    maximum_lead_minutes: int = 30
    minimum_trajectory_confidence: str = "medium"

    def __post_init__(self) -> None:
        if self.minimum_dbz <= 0:
            raise ValueError("severe alert dBZ threshold must be positive")
        if self.maximum_lead_minutes <= 0:
            raise ValueError("severe alert lead time must be positive")
        if self.minimum_trajectory_confidence not in {"low", "medium", "high"}:
            raise ValueError(
                "minimum trajectory confidence must be low, medium, or high"
            )


@dataclass(frozen=True)
class AlertLifecycleSettings:
    expiry: timedelta = timedelta(minutes=20)


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool = False
    bot_token: str | None = None
    chat_id: str | None = None
    request_timeout_seconds: float = 10.0
    maximum_event_age: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        if self.enabled and not self.bot_token:
            raise ValueError("HAIL_TELEGRAM_BOT_TOKEN is required when Telegram is enabled")
        if self.enabled and not self.chat_id:
            raise ValueError("HAIL_TELEGRAM_CHAT_ID is required when Telegram is enabled")
        if self.request_timeout_seconds <= 0:
            raise ValueError("Telegram request timeout must be positive")
        if self.maximum_event_age <= timedelta(0):
            raise ValueError("Telegram maximum event age must be positive")


@dataclass(frozen=True)
class HailRiskAgentSettings:
    """Configuration for optional web-researched hail-risk assessments."""

    enabled: bool = False
    api_key: str | None = None
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "high"
    candidate_minimum_dbz: int = 55
    maximum_lead_minutes: int = 30
    alert_threshold: int = 7
    research_cooldown: timedelta = timedelta(minutes=30)
    request_timeout_seconds: float = 90.0
    max_output_tokens: int = 6000

    def __post_init__(self) -> None:
        if self.enabled and not self.api_key:
            raise ValueError("OPENAI_API_KEY is required when HAIL_LLM_ENABLED is true")
        if not self.model.strip():
            raise ValueError("HAIL_LLM_MODEL must not be empty")
        if self.reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(
                "HAIL_LLM_REASONING_EFFORT must be none, low, medium, high, xhigh, or max"
            )
        if self.candidate_minimum_dbz <= 0:
            raise ValueError("HAIL_LLM_CANDIDATE_MINIMUM_DBZ must be positive")
        if self.maximum_lead_minutes <= 0:
            raise ValueError("HAIL_LLM_MAXIMUM_LEAD_MINUTES must be positive")
        if not 0 <= self.alert_threshold <= 10:
            raise ValueError("HAIL_LLM_ALERT_THRESHOLD must be between 0 and 10")
        if self.research_cooldown <= timedelta(0):
            raise ValueError("HAIL_LLM_RESEARCH_COOLDOWN_MINUTES must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("HAIL_LLM_TIMEOUT_SECONDS must be positive")
        if self.max_output_tokens <= 0:
            raise ValueError("HAIL_LLM_MAX_OUTPUT_TOKENS must be positive")


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for OPERA composite and Craiova volume ingestion."""

    archive_root: Path
    poll_interval_seconds: int
    tracker_maximum_gap: timedelta
    tracker_maximum_speed_kmh: int
    nearby_track_poll_interval_seconds: int = 600
    nearby_track_distance_km: float = 100.0
    nearby_track_hold_updates: int = 5
    frame_retention: timedelta = timedelta(hours=24)
    tracker_motion_window_observations: int = 5
    target: TargetLocation = field(default_factory=TargetLocation)
    forecast_horizon: timedelta = timedelta(minutes=120)
    severe_alert: SevereAlertSettings = field(default_factory=SevereAlertSettings)
    alert_lifecycle: AlertLifecycleSettings = field(default_factory=AlertLifecycleSettings)
    opera_enabled: bool = True
    opera_api_url: str = DEFAULT_OPERA_API_URL
    opera_lookback: timedelta = timedelta(minutes=70)
    opera_maximum_frame_age: timedelta = timedelta(minutes=15)
    opera_segmentation_minimum_dbz: int = 35
    opera_segmentation_minimum_pixels: int = 2
    opera_centroid_weight_exponent: float = 3.0
    opera_storm_minimum_area_square_km: float = 30.0
    opera_storm_minimum_45dbz_core_square_km: float = 8.0
    opera_storm_minimum_55dbz_core_square_km: float = 1.0
    opera_track_minimum_confirmations: int = 2
    opera_volume_enabled: bool = True
    opera_volume_api_url: str = DEFAULT_OPERA_VOLUME_API_URL
    opera_volume_radar_id: str = DEFAULT_OPERA_VOLUME_RADAR_ID
    opera_volume_radar_name: str = DEFAULT_OPERA_VOLUME_RADAR_NAME
    opera_volume_lookback: timedelta = timedelta(minutes=15)
    opera_volume_maximum_frame_age: timedelta = timedelta(minutes=15)
    opera_volume_maximum_beam_height_metres: int = 20_000
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    hail_risk_agent: HailRiskAgentSettings = field(default_factory=HailRiskAgentSettings)

    @classmethod
    def from_environment(cls) -> "Settings":
        environment_file = Path(os.getenv("HAIL_ENV_FILE", ".env"))
        load_dotenv(environment_file, override=False)
        return cls(
            archive_root=Path(os.getenv("HAIL_ARCHIVE_DIR", "var/radar")),
            poll_interval_seconds=_positive_int("HAIL_POLL_INTERVAL_SECONDS", 1_800),
            tracker_maximum_gap=timedelta(
                minutes=_positive_int("HAIL_TRACKER_MAXIMUM_GAP_MINUTES", 12)
            ),
            tracker_maximum_speed_kmh=_positive_int(
                "HAIL_TRACKER_MAXIMUM_SPEED_KMH", 160
            ),
            nearby_track_poll_interval_seconds=_positive_int(
                "HAIL_NEARBY_TRACK_POLL_INTERVAL_SECONDS", 600
            ),
            nearby_track_distance_km=_positive_float(
                "HAIL_NEARBY_TRACK_DISTANCE_KM", 100.0
            ),
            nearby_track_hold_updates=_positive_int(
                "HAIL_NEARBY_TRACK_HOLD_UPDATES", 5
            ),
            frame_retention=timedelta(
                hours=_positive_int("HAIL_FRAME_RETENTION_HOURS", 24)
            ),
            tracker_motion_window_observations=_positive_int(
                "HAIL_TRACKER_MOTION_WINDOW_OBSERVATIONS", 5
            ),
            target=TargetLocation(
                latitude=float(os.getenv("HAIL_TARGET_LATITUDE", "42.6977")),
                longitude=float(os.getenv("HAIL_TARGET_LONGITUDE", "23.3219")),
                warning_radius_km=_positive_float("HAIL_WARNING_RADIUS_KM", 10.0),
            ),
            forecast_horizon=timedelta(
                minutes=_positive_int("HAIL_FORECAST_HORIZON_MINUTES", 120)
            ),
            severe_alert=SevereAlertSettings(
                minimum_dbz=_positive_int("HAIL_SEVERE_MINIMUM_DBZ", 65),
                maximum_lead_minutes=_positive_int(
                    "HAIL_SEVERE_MAXIMUM_LEAD_MINUTES", 30
                ),
                minimum_trajectory_confidence=os.getenv(
                    "HAIL_MINIMUM_TRAJECTORY_CONFIDENCE", "medium"
                )
                .strip()
                .lower(),
            ),
            alert_lifecycle=AlertLifecycleSettings(
                expiry=timedelta(minutes=_positive_int("HAIL_ALERT_EXPIRY_MINUTES", 20))
            ),
            opera_enabled=_boolean("HAIL_OPERA_ENABLED", True),
            opera_api_url=os.getenv("HAIL_OPERA_API_URL", DEFAULT_OPERA_API_URL),
            opera_lookback=timedelta(
                minutes=_positive_int("HAIL_OPERA_LOOKBACK_MINUTES", 70)
            ),
            opera_maximum_frame_age=timedelta(
                minutes=_positive_int("HAIL_OPERA_MAX_FRAME_AGE_MINUTES", 15)
            ),
            opera_segmentation_minimum_dbz=_positive_int(
                "HAIL_OPERA_SEGMENT_MINIMUM_DBZ", 35
            ),
            opera_segmentation_minimum_pixels=_positive_int(
                "HAIL_OPERA_SEGMENT_MINIMUM_PIXELS", 2
            ),
            opera_centroid_weight_exponent=_positive_float(
                "HAIL_OPERA_CENTROID_WEIGHT_EXPONENT", 3.0
            ),
            opera_storm_minimum_area_square_km=_positive_float(
                "HAIL_OPERA_STORM_MIN_AREA_SQUARE_KM", 30.0
            ),
            opera_storm_minimum_45dbz_core_square_km=_positive_float(
                "HAIL_OPERA_STORM_MIN_45DBZ_CORE_SQUARE_KM", 8.0
            ),
            opera_storm_minimum_55dbz_core_square_km=_positive_float(
                "HAIL_OPERA_STORM_MIN_55DBZ_CORE_SQUARE_KM", 1.0
            ),
            opera_track_minimum_confirmations=_positive_int(
                "HAIL_OPERA_TRACK_MINIMUM_CONFIRMATIONS", 2
            ),
            opera_volume_enabled=_boolean("HAIL_OPERA_VOLUME_ENABLED", True),
            opera_volume_api_url=os.getenv(
                "HAIL_OPERA_VOLUME_API_URL", DEFAULT_OPERA_VOLUME_API_URL
            ),
            opera_volume_radar_id=os.getenv(
                "HAIL_OPERA_VOLUME_RADAR_ID", DEFAULT_OPERA_VOLUME_RADAR_ID
            ),
            opera_volume_radar_name=os.getenv(
                "HAIL_OPERA_VOLUME_RADAR_NAME", DEFAULT_OPERA_VOLUME_RADAR_NAME
            ),
            opera_volume_lookback=timedelta(
                minutes=_positive_int("HAIL_OPERA_VOLUME_LOOKBACK_MINUTES", 15)
            ),
            opera_volume_maximum_frame_age=timedelta(
                minutes=_positive_int("HAIL_OPERA_VOLUME_MAX_FRAME_AGE_MINUTES", 15)
            ),
            opera_volume_maximum_beam_height_metres=_positive_int(
                "HAIL_OPERA_VOLUME_MAX_BEAM_HEIGHT_METRES", 20_000
            ),
            telegram=TelegramSettings(
                enabled=_boolean("HAIL_TELEGRAM_ENABLED", False),
                bot_token=os.getenv("HAIL_TELEGRAM_BOT_TOKEN"),
                chat_id=os.getenv("HAIL_TELEGRAM_CHAT_ID"),
                request_timeout_seconds=_positive_float(
                    "HAIL_TELEGRAM_TIMEOUT_SECONDS", 10.0
                ),
                maximum_event_age=timedelta(
                    minutes=_positive_int("HAIL_TELEGRAM_MAX_EVENT_AGE_MINUTES", 10)
                ),
            ),
            hail_risk_agent=HailRiskAgentSettings(
                enabled=_boolean("HAIL_LLM_ENABLED", False),
                api_key=os.getenv("OPENAI_API_KEY"),
                model=os.getenv("HAIL_LLM_MODEL", "gpt-5.6-sol"),
                reasoning_effort=os.getenv("HAIL_LLM_REASONING_EFFORT", "high")
                .strip()
                .lower(),
                candidate_minimum_dbz=_positive_int(
                    "HAIL_LLM_CANDIDATE_MINIMUM_DBZ", 55
                ),
                maximum_lead_minutes=_positive_int(
                    "HAIL_LLM_MAXIMUM_LEAD_MINUTES", 30
                ),
                alert_threshold=_bounded_int(
                    "HAIL_LLM_ALERT_THRESHOLD", 7, minimum=0, maximum=10
                ),
                research_cooldown=timedelta(
                    minutes=_positive_int("HAIL_LLM_RESEARCH_COOLDOWN_MINUTES", 30)
                ),
                request_timeout_seconds=_positive_float("HAIL_LLM_TIMEOUT_SECONDS", 90.0),
                max_output_tokens=_positive_int("HAIL_LLM_MAX_OUTPUT_TOKENS", 6000),
            ),
        )
