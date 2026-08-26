# EUMETNET OPERA primary-source investigation

**Status:** primary observation, tracking, readiness, and alert source.

## Confirmed access path

| Item | Value |
| --- | --- |
| API | <https://api.meteogate.eu/eu-eumetnet-weather-radar/docs> |
| Composite location | `0-20010-0-OPERA` |
| Query | `standard_name=DBZH`, `format=ODIM`, `method=comp`, `f=CoverageJSON` |
| Data links | `application/x-odim` HDF5 files in the public 24-hour S3 cache |
| Product | OPERA CIRRUS DBZH maximum-reflectivity composite |
| Cadence observed | five minutes |

The composite is an ODIM HDF5 `COMP` product with `quantity=DBZH` and
`product=MAX`. Its 1 km grid and supplied Lambert azimuthal equal-area
projection are used directly for cell segmentation, tracking, and the Sofia
overlay. No values are interpolated for the visual layer.

## Operational rule

Raw segmentation retains 35 dBZ components of two or more native pixels. A
component becomes an operational storm if it has either a broad 45 dBZ core or
a compact 55 dBZ core. Confirmed tracks use a recency-weighted motion fit.

Telegram delivery occurs only from fresh, complete OPERA processing when a
confirmed track has a 65+ dBZ maximum band and a medium-or-higher confidence
trajectory entering the configured target radius within 30 minutes. The signal
is labelled as severe reflectivity, not confirmed hail or an official warning.
