from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import os
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_MANIFEST_URL = "https://www.weathermod-bg.eu/wr/json/JAR.json"
DEFAULT_OPERA_API_URL = (
    "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/"
    "observations/locations/0-20010-0-OPERA"
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
    """A fixed target point; defaults are development-only Sofia centre values."""

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
class HailPolicySettings:
    """Conservative radar-signature policy; not an official warning policy."""

    high_dbz_calibration_validated: bool = False
    watch_minimum_dbz: int = 55
    warning_minimum_dbz: int = 60
    severe_minimum_dbz: int = 65
    watch_minimum_lead_minutes: int = 30
    watch_maximum_lead_minutes: int = 90
    warning_minimum_lead_minutes: int = 10
    warning_maximum_lead_minutes: int = 45

    def __post_init__(self) -> None:
        if not 0 < self.watch_minimum_dbz <= self.warning_minimum_dbz <= self.severe_minimum_dbz:
            raise ValueError("hail dBZ thresholds must be increasing positive values")
        if not 0 <= self.watch_minimum_lead_minutes <= self.watch_maximum_lead_minutes:
            raise ValueError("watch lead-time bounds are invalid")
        if not 0 <= self.warning_minimum_lead_minutes <= self.warning_maximum_lead_minutes:
            raise ValueError("warning lead-time bounds are invalid")


@dataclass(frozen=True)
class AlertLifecycleSettings:
    """State-management settings; delivery adapters are intentionally separate."""

    expiry: timedelta = timedelta(minutes=20)


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the ingestion process and status API."""

    archive_root: Path
    manifest_url: str
    source_timezone: ZoneInfo
    maximum_frame_age: timedelta
    poll_interval_seconds: int
    segmentation_minimum_dbz: int
    segmentation_minimum_pixels: int
    tracker_maximum_gap: timedelta
    tracker_maximum_speed_kmh: int
    tracker_motion_window_observations: int = 5
    target: TargetLocation = field(default_factory=TargetLocation)
    forecast_horizon: timedelta = timedelta(minutes=120)
    hail_policy: HailPolicySettings = field(default_factory=HailPolicySettings)
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
    yarlovo_analysis_enabled: bool = False

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            archive_root=Path(os.getenv("HAIL_ARCHIVE_DIR", "var/radar")),
            manifest_url=os.getenv("HAIL_MANIFEST_URL", DEFAULT_MANIFEST_URL),
            # Treating a potentially UTC source label as Sofia time is conservative:
            # it can make a valid frame look older, never newer.
            source_timezone=ZoneInfo(os.getenv("HAIL_SOURCE_TIMEZONE", "Europe/Sofia")),
            maximum_frame_age=timedelta(
                minutes=_positive_int("HAIL_MAX_FRAME_AGE_MINUTES", 10)
            ),
            poll_interval_seconds=_positive_int("HAIL_POLL_INTERVAL_SECONDS", 240),
            segmentation_minimum_dbz=_positive_int("HAIL_SEGMENT_MINIMUM_DBZ", 35),
            segmentation_minimum_pixels=_positive_int("HAIL_SEGMENT_MINIMUM_PIXELS", 4),
            tracker_maximum_gap=timedelta(
                minutes=_positive_int("HAIL_TRACKER_MAXIMUM_GAP_MINUTES", 12)
            ),
            tracker_maximum_speed_kmh=_positive_int(
                "HAIL_TRACKER_MAXIMUM_SPEED_KMH", 160
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
            hail_policy=HailPolicySettings(
                high_dbz_calibration_validated=_boolean(
                    "HAIL_HIGH_DBZ_CALIBRATION_VALIDATED", False
                ),
                watch_minimum_dbz=_positive_int("HAIL_WATCH_MINIMUM_DBZ", 55),
                warning_minimum_dbz=_positive_int("HAIL_WARNING_MINIMUM_DBZ", 60),
                severe_minimum_dbz=_positive_int("HAIL_SEVERE_MINIMUM_DBZ", 65),
                watch_minimum_lead_minutes=_positive_int(
                    "HAIL_WATCH_MINIMUM_LEAD_MINUTES", 30
                ),
                watch_maximum_lead_minutes=_positive_int(
                    "HAIL_WATCH_MAXIMUM_LEAD_MINUTES", 90
                ),
                warning_minimum_lead_minutes=_positive_int(
                    "HAIL_WARNING_MINIMUM_LEAD_MINUTES", 10
                ),
                warning_maximum_lead_minutes=_positive_int(
                    "HAIL_WARNING_MAXIMUM_LEAD_MINUTES", 45
                ),
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
            yarlovo_analysis_enabled=_boolean("HAIL_YARLOVO_ANALYSIS_ENABLED", False),
        )
