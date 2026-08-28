**Verdict: NOT READY — consumer-visible failure(s): C1-interactive**

# Cloud Readiness Assessment: s3://demo-bucket/ocean-temp.zarr

**Input:** `s3://demo-bucket/ocean-temp.zarr` (zarr) · **Assessed:** 2026-08-27T00:00:00Z · **Confidence:** Reduced

## Criteria

| Check | Status | Evidence | Remediation |
|---|---|---|---|
| A-class | PASS | format=zarr, class=cloud-native; consolidated metadata (zarr_version=3) | — |
| B1-open | PASS | requests_to_open=2, bytes_to_open=4096; metadata_walk objects_visited=6, complete=True | — |
| B2-crs | SKIPPED | no machine-readable CRS found; live probe unavailable to confirm further | — |
| B3-semantics | PASS | conventions_attr=True; units_and_name=True | — |
| C1-interactive | FAIL | chunks average 118.4 MB per sampled chunk, far above the 1-4 MB interactive band | rewrite with ~2 MB chunks/tiles aligned to viewport access (e.g. 1x180x360), or publish a lower-resolution overview variable for interactive use |
| C5-codec | WARN | gzip: works, but zstd/blosc dominate gzip/zlib/deflate on every axis | recompress with zstd level 3 plus byte shuffle (faster decode at a similar ratio) |
| D1-range | PASS | HTTP Range (206) confirmed via live probe | — |
| E1-etag | PASS | stable ETag observed across two HEAD probes | — |

## Chunk layout — what it's optimized for

- **`sea_water_temperature`** (map-optimized): This layout stores one full spatial map per chunk and slices time thinly: a full-extent map read at one time step touches a single chunk (cheap), but a full time series at one point touches all 365 chunks (expensive). Good fit for map-at-a-time workflows; poor fit for point-timeseries extraction.

## Budget / stage breaches

No budget breaches — every stage completed within its byte/time cap.

## Prioritized remediation

1. **[C1-interactive, 10 pts at stake]** rewrite with ~2 MB chunks/tiles aligned to viewport access (e.g. 1x180x360), or publish a lower-resolution overview variable for interactive use
2. **[C5-codec, 5 pts at stake]** recompress with zstd level 3 plus byte shuffle (faster decode at a similar ratio)

## Sources & rubric provenance

Scores were derived from these published checklists and guidance:

- Cloud-Optimized Geospatial Formats Guide (guide.cloudnativegeo.org) —
  COG, Zarr, and cloud-optimized HDF5/NetCDF checklists
- rio-cogeo validation rules for COG
- NASA ESDIS / IMPACT cloud-optimization guidance for HDF5 (paged
  aggregation, ~8 MB page size, metadata locality)
- Pangeo community chunking guidance for analysis-ready, cloud-optimized
  (ARCO) data
- Zarr v3 specification (sharding codec, consolidated metadata)
- STAC best practices (asset roles, projection/raster extensions)

Machine-readable evidence: `findings.json` alongside this report.
