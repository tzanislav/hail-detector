## Goal

Build a small automated service that monitors storms approaching **Sofia, Bulgaria** and sends a warning when there is a meaningful risk of hail within roughly **30–60 minutes**.

The system should prioritize **low false-positive rates** over warning about every thunderstorm.

## Primary radar source

Use the public **Yarlovo (JAR) weather radar** operated by the Bulgarian Executive Agency for Hail Suppression:

* Radar system: https://www.weathermod-bg.eu/radar-system
* Yarlovo viewer: https://www.weathermod-bg.eu/wr/jar-bg
* Public product appears to be **CAPPI reflectivity at 2.5 km altitude**
* Radar scans are approximately every **4 minutes**

First investigate how the Yarlovo webpage retrieves its current and historical radar frames. Prefer a stable image/data endpoint over browser automation or screenshots.

## Initial hail detection logic

Extract approximate reflectivity (`dBZ`) from each radar frame using the published color scale.

Candidate thresholds:

* `55–60 dBZ` → possible hail
* `60–65 dBZ` → likely hail / possible large hail
* `>65 dBZ` → strong severe-hail signal

Do **not** alert based only on maximum dBZ.

For each detected storm cell track:

* geographic position / centroid
* maximum dBZ
* area above 55 dBZ
* area above 60 dBZ
* growth or weakening over time
* direction of travel
* speed
* projected path
* estimated closest approach to Sofia

Track cells between consecutive ~4-minute radar frames.

## Alerts

Two levels:

### Hail Watch

Approximately **30–90 min** ahead.

Trigger when a strong/intensifying storm is moving toward Sofia and conditions suggest significant hail potential.

### Hail Warning

Approximately **10–45 min** ahead.

Trigger when a probable hail-producing radar core is already present and its projected trajectory intersects the configured Sofia location/radius.

Alert should contain:

* current storm distance
* direction
* movement speed
* maximum observed dBZ
* confidence level
* estimated arrival window
* whether the storm is strengthening or weakening

## Sofia target

Make the target configurable:

```text
latitude
longitude
warning_radius_km
```

Initial use case is a fixed location in Sofia rather than city-wide alerts.

## Secondary data — later phase

Design the code so additional signals can later be incorporated:

* EUMETSAT MTG Lightning Imager
* lightning-rate / lightning-jump detection
* ICON-EU / ECMWF model data
* CAPE
* CIN
* freezing level
* lapse rates
* wind shear
* Sofia radiosonde observations
* official NIMH severe-weather warnings

These should modify a hail-confidence score rather than replace the radar detection.

Example conceptual score:

```text
hail_score =
    radar_intensity
  + high_dbz_area
  + cell_intensification
  + trajectory_confidence
  + lightning_activity
  + atmospheric_hail_potential
```

## Suggested architecture

Keep the first version simple:

```text
Radar Fetcher
    ↓
Image → dBZ conversion
    ↓
Storm-cell segmentation
    ↓
Cell tracking between frames
    ↓
Trajectory prediction
    ↓
Hail probability / confidence
    ↓
Location intersection test
    ↓
Notification service
```

Store enough historical radar frames and detections to debug false positives and tune thresholds.

## Important requirements

* Fetch data automatically every ~4 minutes.
* Avoid alerting repeatedly for the same tracked storm.
* Maintain persistent storm IDs where possible.
* Account for radar/image georeferencing correctly.
* Log all decisions leading to an alert.
* Make thresholds configurable.
* Fail safely if radar data becomes stale.
* Clearly distinguish **observed radar hail signature** from **forecast hail risk**.
* Do not present the system as an official meteorological warning service.

## First development task

Before building the detection system:

1. Inspect the Yarlovo webpage/network requests.
2. Identify the endpoint(s) supplying radar frames.
3. Determine available historical-frame depth.
4. Determine image dimensions, geographic bounds/projection and color legend.
5. Save several consecutive raw frames.
6. Verify that radar pixels can reliably be mapped to:

   * latitude/longitude
   * approximate dBZ.

Produce a short technical report of those findings before implementing the storm tracker.
