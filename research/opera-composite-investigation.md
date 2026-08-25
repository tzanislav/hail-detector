# EUMETNET OPERA primary-source investigation

**Status:** promoted to the primary observation, cell-tracking, map, and
readiness source on 2026-08-25. It is not used by the hail policy or alert
lifecycle.

## Confirmed access path

| Item | Value |
| --- | --- |
| API | <https://api.meteogate.eu/eu-eumetnet-weather-radar/docs> |
| Composite location | `0-20010-0-OPERA` |
| Query | `standard_name=DBZH`, `format=ODIM`, `method=comp`, `f=CoverageJSON` |
| Data links | `application/x-odim` HDF5 files in the public 24-hour S3 cache |
| Product | OPERA CIRRUS maximum-reflectivity composite |
| Cadence observed | five minutes |

The API returns `items` links such as
`OPERA@20260825T1100@0@DBZH.h5`.  Timestamps in the filename and ODIM
metadata are UTC.

## Verified file characteristics

A current 2026-08-25 composite was inspected directly:

- ODIM HDF5 (`ODIM_H5/V2_4`), `object=COMP`;
- `quantity=DBZH`, `product=MAX`;
- 3,800 × 4,400 pixels at 1,000 m grid spacing;
- Lambert azimuthal equal-area projection supplied in the `where/projdef`
  attribute;
- `nodata`, `undetect`, gain, and offset values supplied by the file.

The service uses that projection to nearest-neighbour crop/reproject the
Sofia/Yarlovo map bounds into a transparent 720 × 720 PNG. No raw grid values
are interpolated, so the visual overlay never invents reflectivity between
OPERA cells.

## Initial live check

At 11:04 UTC on 2026-08-25 the API provided a fresh 11:00 UTC composite.  The
Sofia crop had a 64 dBZ maximum and showed the two active cells west of Sofia
seen in the contemporaneous Windy screenshot.  Twelve recent original HDF5
files and their derived local overlays were preserved below `var/radar/opera/`.

This confirms OPERA is a valuable primary observation and tracking feed for
this region. Yarlovo is retained as a local CAPPI comparison and calibration
feed.

## Operational echo filter

Raw segmentation remains deliberately sensitive (35 dBZ components of two or
more native one-kilometre pixels) so source evidence is not silently lost.
Each accepted cell's centroid is intensity-weighted using
`(1 + dBZ - 35)^3`, rather than a plain geometric average, so its track and
forecast follow the strongest embedded core. The 35 dBZ floor is subtracted
before cubing because dBZ is logarithmic; this treats the exponent as a
practical core-emphasis control rather than a physical reflectivity conversion.
Before a component is allowed onto the operational map or into tracking, it
must either have at least 30 km² total area plus an 8 km² >=45 dBZ core, or a
compact >=55 dBZ core of at least 1 km². A track must also be observed in two
frames before it is shown or projected, and it is retired after the configured
12-minute association gap. Rejected components are written alongside accepted
cells in each archived OPERA cell JSON; this filter is a practical echo-quality
gate, not a hail classification.

## Safety boundary

The OPERA product is **maximum reflectivity through the contributing radar
column**, while the Yarlovo product used by the detector is a 2.5 km CAPPI
slice.  Their values must not be blended and Yarlovo's provisional hail
thresholds must not be applied to OPERA. The integration therefore:

1. fetches, integrity-archives, and checks OPERA freshness as the `/health`
   readiness source;
2. segments native 1 km DBZH grid cells and tracks them independently;
3. renders a clearly labelled primary dashboard layer; and
4. keeps all hail candidates and automatic alerts disabled.

Any later OPERA cell segmentation or confidence fusion needs a separately
validated policy and labelled historical event set.
