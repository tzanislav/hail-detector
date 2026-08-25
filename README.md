# Hail detector

This repository uses **EUMETNET OPERA DBZH maximum-reflectivity composites**
as its primary observation, cell-tracking, map, and readiness source.
**Yarlovo CAPPI** is preserved as a local secondary comparison and calibration
source. Automatic hail candidates and alerts are deliberately disabled while
the distinct OPERA product is calibrated against labelled events.

The service fetches the public OPERA composite API, preserves the original ODIM HDF5 files, checks feed age conservatively, segments significant native-grid DBZH cells, and links them into storm tracks. It preserves Yarlovo CAPPI PNGs and their colour-band calibration separately. The source investigations are in [`research/opera-composite-investigation.md`](research/opera-composite-investigation.md) and [`research/yarlovo-radar-investigation.md`](research/yarlovo-radar-investigation.md).

## Run locally

Use two processes in separate terminals:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m hail_detector.poller
```

```powershell
.\.venv\Scripts\uvicorn.exe hail_detector.api:app --app-dir src --reload
```

The debug dashboard is at `http://127.0.0.1:8000/`. Its **Fetch latest** button performs an immediate primary-OPERA poll; the regular poller should still remain running for continuous updates. The status API is at `http://127.0.0.1:8000/status`; `/tracks` exposes the primary OPERA cell-track state; `/health` returns HTTP 503 until a fresh OPERA composite has been archived and analysed.

The dashboard uses Leaflet and OpenStreetMap tiles to display the reprojected OPERA primary overlay, the Yarlovo secondary CAPPI overlay, the configured target radius, OPERA-segmented cells, tracks, projections, and alert boundary. The map's lower-edge timeline can scrub through whatever locally archived OPERA overlays exist from the last 24 hours; **Live** restores the current OPERA and Yarlovo view with its forecasts. OPERA maximum reflectivity is clearly labelled and no automatic hail decision is produced from it yet. A stale OPERA feed is intentionally shown as a prominent amber warning, not as a healthy live detection view.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `HAIL_ARCHIVE_DIR` | `var/radar` | Runtime archive location. |
| `HAIL_MANIFEST_URL` | Yarlovo active JSON manifest | Override only for testing or a verified source change. |
| `HAIL_SOURCE_TIMEZONE` | `Europe/Sofia` | Timezone applied to the source filename timestamp. This fails safely if the source's timezone remains uncertain. |
| `HAIL_MAX_FRAME_AGE_MINUTES` | `10` | A newer valid CAPPI frame is required for a `ready` status. |
| `HAIL_POLL_INTERVAL_SECONDS` | `240` | Poll cadence. |
| `HAIL_OPERA_ENABLED` | `true` | Enable the primary EUMETNET OPERA observation poller. `/health` follows this source. |
| `HAIL_OPERA_API_URL` | OPERA DBZH composite API | Override only for a verified OPERA endpoint change. |
| `HAIL_OPERA_LOOKBACK_MINUTES` | `70` | Recent OPERA window to archive on each poll. |
| `HAIL_OPERA_MAX_FRAME_AGE_MINUTES` | `15` | Freshness threshold required for primary OPERA readiness. |
| `HAIL_OPERA_SEGMENT_MINIMUM_DBZ` / `HAIL_OPERA_SEGMENT_MINIMUM_PIXELS` | `35` / `2` | Sensitive raw-component threshold. Every resulting component is archived, but not necessarily shown or tracked as a storm. |
| `HAIL_OPERA_CENTROID_WEIGHT_EXPONENT` | `3` | OPERA cell centroid weights each native pixel by `(1 + dBZ - segmentation floor)^exponent`. The default cubic weighting pulls the track toward the strongest embedded radar core; changing it rebuilds the recent track window. |
| `HAIL_OPERA_STORM_MIN_AREA_SQUARE_KM` / `HAIL_OPERA_STORM_MIN_45DBZ_CORE_SQUARE_KM` | `30` / `8` | An operational storm must meet both the broad-area and >=45 dBZ-core requirement, unless it passes the compact >=55 dBZ-core exception below. |
| `HAIL_OPERA_STORM_MIN_55DBZ_CORE_SQUARE_KM` | `1` | A compact high-reflectivity core of this size is retained even if its wider component is small. This is still not a hail threshold. |
| `HAIL_OPERA_TRACK_MINIMUM_CONFIRMATIONS` | `2` | Observations required before an OPERA track is shown on the dashboard or forecast. One-scan candidate tracks remain in the archived tracker state. |
| `HAIL_YARLOVO_ANALYSIS_ENABLED` | `false` | Keep the secondary Yarlovo tracker and alert lifecycle disabled. Do not enable it while OPERA is primary. |
| `HAIL_SEGMENT_MINIMUM_DBZ` | `35` | Lower dBZ bound required to include a validated pixel in a storm cell. |
| `HAIL_SEGMENT_MINIMUM_PIXELS` | `4` | Smallest retained 8-connected cell. |
| `HAIL_TRACKER_MAXIMUM_GAP_MINUTES` | `12` | Longest gap over which a cell can retain its storm-track ID. |
| `HAIL_TRACKER_MAXIMUM_SPEED_KMH` | `160` | Association speed gate; this is not a weather forecast. |
| `HAIL_TRACKER_MOTION_WINDOW_OBSERVATIONS` | `5` | Recent observations used by the recency-weighted motion fit. Raw centroids stay archived; the map path and trajectory use the fitted motion. |
| `HAIL_TARGET_LATITUDE` / `HAIL_TARGET_LONGITUDE` | Sofia centre (`42.6977`, `23.3219`) | Development defaults for the fixed target. Set these to the actual protected location before alerts are built. |
| `HAIL_WARNING_RADIUS_KM` | `10` | Target-radius intersection used by the projection layer. |
| `HAIL_FORECAST_HORIZON_MINUTES` | `120` | Maximum constant-velocity projection horizon. Predictions require at least three observations; the map then marks +30, +60, and +120 minute positions when available. |
| `HAIL_HIGH_DBZ_CALIBRATION_VALIDATED` | `false` | Safety gate: candidate Watches/Warnings remain suppressed until real 55+ dBZ PNG colours are verified. |
| `HAIL_WATCH_MINIMUM_DBZ` / `HAIL_WARNING_MINIMUM_DBZ` / `HAIL_SEVERE_MINIMUM_DBZ` | `55` / `60` / `65` | Conservative lower-bound dBZ thresholds for the provisional radar-signature policy. |
| `HAIL_ALERT_EXPIRY_MINUTES` | `20` | Fresh non-qualifying observations required before an active candidate alert expires. Stale data never expires alerts. |

Runtime files under `var/` are intentionally ignored by Git. The poller stores raw OPERA HDF5 composites, native-grid OPERA cells/tracks, their Sofia map overlays, and separate Yarlovo local CAPPI comparison archives there. `GET /opera/history` exposes available locally archived OPERA overlays for a requested 1–24 hour window, so the dashboard timeline naturally fills as polling continues. Each OPERA cell JSON records accepted `cells` separately from raw `rejected_cells`, so echo filtering remains auditable.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The tests use the saved Yarlovo frames and do not call the live radar endpoint.

## Safety boundary

This milestone returns observed **radar reflectivity**, not confirmed hail. It exposes a short-horizon constant-velocity OPERA cell-track projection, clearly separate from a meteorological forecast or official warning. OPERA is a maximum-reflectivity composite rather than Yarlovo's 2.5 km CAPPI slice, so all automatic hail candidates and alert delivery are disabled pending a separately validated OPERA policy.

The notification layer currently writes transition events to a local file outbox only. It deduplicates unchanged candidates, escalates a Watch to a Warning, records de-escalation/expiry, and refuses to mutate state when data is stale. An external channel such as Telegram will be added only after its credentials and delivery policy are chosen.
