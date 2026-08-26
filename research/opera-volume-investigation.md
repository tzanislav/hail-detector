# OPERA volume-radar feasibility investigation

**Investigation date:** 2026-08-25  
**Status:** experimental secondary observation; never used for the primary
track, trajectory, health endpoint, hail policy, or alerts.

## Availability near Sofia

The public OPERA location catalogue was queried directly. It does not currently
list a Bulgarian polar-volume radar. The closest available volume station is:

| Item | Confirmed value |
| --- | --- |
| OPERA platform | `0-642-0-rocra` |
| Location | Craiova, Romania (`44.310293, 23.867430`) |
| Distance to Sofia centre | 184.5 km |
| Fields advertised | `DBZH:scan`, `TH:scan`, `VRADH:scan` |
| Latest checked DBZH volume | `rocra@20260825T1345@...@DBZH.h5` |
| Volume structure | 12 elevation scans, 360 rays, 920 bins, 250 m bin spacing |
| Licence shown by the API | CC BY 4.0 |

The query is the public Open Radar Data location endpoint with
`standard_name=DBZH`, `format=ODIM`, `method=scan`, and `f=CoverageJSON`.
Unlike the OPERA composite response, its download links live inside the
response's `coverages[*].links` arrays.

## What we added

`opera_volume.py` fetches, integrity-archives, and samples the DBZH PVOL HDF5
files. For every confirmed primary OPERA track it records a vertical profile
at the current track centroid. Each profile value is the maximum over a small
3-ray by 3-gate neighbourhood, not a fictitious exact point measurement.

The dashboard shows that profile on the track card as **Experimental vertical
profile**, including the radar distance and sampled beam height. The compact
status strip includes the volume scan age. `GET /opera/volume/status` exposes
the complete status and per-track profile. The normal **Fetch latest** action
also refreshes the volume source.

## Essential limitation

At Sofia's centre the lowest 0.5-degree beam is approximately **3.8 km MSL**
from Craiova. Higher scan elevations sample progressively higher layers. This
cannot observe the low-level storm structure over Sofia, and the intervening
terrain has not been modelled. It is useful only as a corroborating view of a
storm's vertical reflectivity, especially for cells closer to Romania.

It must not be treated as a local Bulgarian volume scan. For that reason the
integration is deliberately isolated from tracking, trajectory fitting,
readiness, and all hail decisions. VRADH is advertised by the station but has
not been integrated yet: a single distant radar provides only radial velocity,
so it needs separate geometry and quality validation before it can support a
motion estimate.
