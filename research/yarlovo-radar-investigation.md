# Yarlovo radar source investigation

**Status:** completed before storm-tracker implementation  
**Investigation window:** 2026-08-25, approximately 10:17–10:22 UTC  
**Scope:** determine how the public Yarlovo viewer supplies frames, its useful history, and whether the rendered pixels can be converted to location and approximate reflectivity.

## Result

The public viewer uses direct HTTP JSON and PNG resources. Browser automation and screenshots are not needed.

The initial adapter should use the viewer's active JSON manifest, download only entries whose filename identifies a `CAP` product, and store the original PNG before processing it. The image overlay is georeferenced by explicit Leaflet bounds in the page source, and the opaque PNG colours match the published reflectivity legend well enough to return **dBZ bands**. They do **not** support a claim of continuous, exact dBZ values.

The source was stale during this investigation, so source-age handling is a release-blocking safety requirement.

## Sources and endpoints

| Purpose | Endpoint | Finding |
| --- | --- | --- |
| Viewer | <https://www.weathermod-bg.eu/wr/jar-bg> | Its inline JavaScript performs the active manifest request and creates the image overlay. It labels the product as reflectivity at 2.5 km altitude. |
| Active manifest (recommended primary) | `https://www.weathermod-bg.eu/wr/json/JAR.json?ts=<cache-buster>` | Returns `{"img_list": {…}}`, with `wh_img` (absolute PNG URL) and `timp` (display timestamp) for each item. It was the endpoint actually called by the viewer and returned HTTP `ETag` and `Last-Modified` headers. |
| Legacy manifest (use only as a guarded bootstrap/fallback) | `https://www.weathermod-bg.eu/wr/img_data.php?srs=JAR` | Currently returns the same schema with more frames, but this request is commented out in the current viewer source and has no observed cache validators. Do not make it the only production dependency without a longer stability check. |
| Frame archive host | `https://wr.weathermod-bg.eu/data_png_mp/<filename>.png` | Direct PNG downloads work. Directory listing at `…/data_png_mp/` returns HTTP 403, so filenames cannot safely be enumerated from the directory. |
| Colour legend | <https://www.weathermod-bg.eu/wr/img/legenda_rad.jpg> | A 183 × 392 JPEG containing the published reflectivity bands. |
| Radar-system description | <https://www.weathermod-bg.eu/radar-system> | States that an automatic full-volume scan and archive occur every four minutes and describes CAPPI as constant-altitude reflectivity. |

The active manifest's keys are not a reliable chronological contract; parse the timestamp/filename and sort explicitly. Use a cache-busting query parameter as the viewer does, while retaining the response `ETag` when it is available.

## Available history and product filtering

At capture time the active manifest contained 15 items:

- 13 CAP entries from `25.08.2026 06:33` through `07:17` (about **44 minutes** of usable CAP history).
- Two `RN…` entries: one interleaved at `07:00` and one newer item at `08:00`. Neither is a CAP product and neither may be fed to the CAPPI storm tracker.

The working legacy manifest contained 45 items:

- 41 CAP entries from `04:49` through `07:17` (about **2 h 28 min** of usable CAP history).
- Four interleaved `RN…` entries at hourly times. At `05:00`, both an `RN…` and a `CAP…` item appear, which confirms that list position and minute-rounded display time alone are not sufficient selection rules.

The five saved consecutive CAP filenames encode source times with seconds and show a regular 222–223 second cadence (3 min 42–43 sec):

```text
JAR260825070252.CAP20YE.png
JAR260825070635.CAP20YM.png
JAR260825071017.CAP20YU.png
JAR260825071359.CAP20Z1.png
JAR260825071742.CAP20Z8.png
```

**Adapter rule:** select only filenames matching `^JAR\d{12}\.CAP[^/]*\.png$`. Persist the full filename, manifest `timp`, parsed filename timestamp, fetch timestamp, and SHA-256. Do not use `RN` products until their product meaning and calibration are investigated separately.

## Frame format and georeferencing

All five downloaded CAP samples are 720 × 720 PNGs (`Format32bppArgb`, 96 DPI). The source page uses Leaflet's `L.imageOverlay()` with these active bounds:

```text
south-west: latitude 40.674, longitude 20.678
north-east: latitude 44.455, longitude 25.822
```

The source passes the two corners in reverse order, but Leaflet normalizes them into the bounds above. Because Leaflet's normal CRS is Web Mercator, map the image row in **Web Mercator Y**, not by a naive linear latitude interpolation.

For a pixel centre at column `c` and row `r` (zero-based, `0 ≤ c,r < 720`), use:

```text
f_x = (c + 0.5) / 720
f_y = (r + 0.5) / 720

longitude = west + f_x × (east - west)
mercator_y = mercator_y(north) + f_y × (mercator_y(south) - mercator_y(north))
latitude = inverse_web_mercator(mercator_y)
```

This produces an overlay resolution of approximately **795.3 Web-Mercator metres/pixel east–west** and **794.1 Web-Mercator metres/pixel north–south**. Those are projected distances, not physical ground distances. Near Sofia city centre the physical spacing is approximately **586 m east–west** and **585 m north–south** per pixel. As a sanity check, Sofia city centre (`42.6977, 23.3219`) maps to approximately **column 370.1, row 340.1**.

This verifies a reliable mapping to the public viewer's displayed geography. It is not a substitute for scientific radar metadata or ground-control-point validation, so track/arrival uncertainty must be at least on the order of a pixel plus cell-segmentation error.

## Reflectivity / dBZ calibration

The PNGs are transparent overlays, not map screenshots:

- Most pixels have alpha `0` and arbitrary RGB values. They mean **no displayed echo** and must be discarded by alpha before any colour lookup.
- Opaque pixels have alpha `255` and use discrete colour classes. The current dry-weather sample contained only 153–483 opaque pixels per frame out of 518,400.

The following raw PNG colours were observed and agree with the low-to-midrange bands in the published legend:

| Raw opaque RGB | Legend band | Observation |
| --- | --- | --- |
| `#A6F7FF` | 5–10 dBZ | observed |
| `#45AEFA` | 10–15 dBZ | observed |
| `#29D7FF` | 15–20 dBZ | observed |
| `#24FF24` | 20–25 dBZ | observed |
| `#00C037` | 25–30 dBZ | observed |
| `#008227` | 30–35 dBZ | observed |
| `#FFFF1E` | 35–40 dBZ | observed |
| `#FFAE00` | 40–45 dBZ | observed |
| `#ED0000` | 45–50 dBZ | observed |

The legend also defines the alert-relevant higher bands: 50–55, 55–60, 60–63, 63–65, 65–67, 67–70, and a highest saturated `≥70`-ish purple band (the legend labels its upper end `>73`). No opaque pixel in the collected quiet-weather samples reached 50 dBZ or higher, so the exact raw RGB values for those classes have **not** yet been validated against an actual CAP frame.

### Calibration decision

The first implementation may use an exact raw-RGB lookup table that returns a **closed dBZ interval** (for example, `[55, 60)`) and a representative score value if needed. It must not invent precision such as `57.3 dBZ`. Unknown opaque colours should be logged and assigned no hail class until reviewed. A future active-convection capture is required before enabling a 55+ dBZ alert policy in production.

## Raw artifacts saved

Artifacts are under [`frames/`](./yarlovo/frames/). They are the original bytes used for this report.

| File | Size | SHA-256 |
| --- | ---: | --- |
| `JAR.manifest.json` | 1,700 B | `F4DB0A2A2E9FE4A8D284F7F5965ECF0EB902689DE74D1640FF0EC9F4B2DEF5A3` |
| `JAR.legacy-manifest.json` | 5,094 B | `2A5DEC21C209A1ED90C67BF6DAC5377B1617E926AF0AF3360988CA5949C19D9A` |
| `JAR260825070252.CAP20YE.png` | 7,749 B | `4DC106F9D2C883A92E82262082AC8D79BFF965382D6013486376C67C9EFC617F` |
| `JAR260825070635.CAP20YM.png` | 7,475 B | `93A48C553368EAC8FC243434405F4CB3852CFBC366AFF5C118A3A4E3A7E64CAD` |
| `JAR260825071017.CAP20YU.png` | 7,334 B | `A78F197BF4B7EF4DE89FD8C1CFC310EB7180B366ED428ADF9D7C60CBBB47894B` |
| `JAR260825071359.CAP20Z1.png` | 7,499 B | `FF15F099DE8C332405CEEF990977C801B903D138D5932AFC5482949E240B0FD4` |
| `JAR260825071742.CAP20Z8.png` | 7,263 B | `0C736C7575BAF0B356BC0926FD00F6A452A6A060582AEC203293E6123C1E8667` |
| `legenda_rad.jpg` | 38,873 B | `4A5A5617B8A0D790A5E079E3F71DFEB128259205C5DED126B41E503E51CA40F5` |

## Staleness and operational risks

During the investigation, the manifest was refreshed around 10:17–10:22 UTC, but its newest valid CAP timestamp was `07:17:42`. Even if the displayed timestamp is assumed to be UTC, that is more than three hours old. The manifest's newest item was the non-CAP `RN` item at `08:00`.

The timestamp timezone is not explicitly documented by the endpoint and HTTP `Last-Modified` values did not agree with the displayed observation time. Therefore:

1. Store the source timestamp unmodified and the UTC fetch time separately.
2. Make the source timestamp timezone an explicit adapter setting, then validate it during a live scan cycle.
3. Treat the feed as stale and suppress new watches/warnings when the newest valid CAP frame exceeds the configured maximum age (initially 10 minutes is appropriate for a four-minute source).
4. Expose stale status prominently in the health endpoint and notification audit log.

This is essential: an apparent latest image is not necessarily a recent CAPPI observation.

## Recommended next implementation step

Implement a small Yarlovo ingestion/calibration slice, not the storm tracker yet:

1. Fetch and parse the active manifest on an interval with jitter.
2. Filter CAP filenames, order by parsed source timestamp, reject duplicates, and enforce staleness.
3. Download and archive every accepted raw PNG plus metadata.
4. Implement the Web-Mercator pixel-to-coordinate transform above.
5. Implement alpha masking and the currently validated 5–50 dBZ colour bins; emit explicit `unvalidated_high_bin` diagnostics for higher classes until a storm sample is captured.

Only after this slice is testable against the saved frames should cell segmentation and tracking begin.
