**Verdict: NOT READY — consumer-visible failure(s): C1-interactive**
Tier C · 71.5/100 · confidence Reduced

# Cloud Readiness Assessment: s3://demo-bucket/ocean-temp.zarr

**Input:** `s3://demo-bucket/ocean-temp.zarr` (zarr) · **Assessed:** 2026-08-27T00:00:00Z · **Confidence:** Reduced

## Scorecard

| Dimension | What | Score | Max |
|---|---|---|---|
| A | Format & structure | 26.0 | 30 |
| B | Metadata locality & richness | 14.5 | 20 |
| C | Chunking & AI-workflow fit | 12.0 | 25 |
| D | Access & transport | 8.0 | 15 |
| E | Reproducibility & governance | 5.0 | 10 |
| **Total** | | **71.5** | **100** |
| Metadata dispersal | requests-to-open=2; full-metadata-walk=6 requests (objects_visited=6, complete=True) | — | — |

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

## Per-asset findings

### `s3://demo-bucket/ocean-temp.zarr` — zarr — 71.5/100 (C)

| Check | Dim | Status | Pts | Evidence |
|---|---|---|---|---|
| A-class | A | pass | 26.0/30 | format=zarr, class=cloud-native; consolidated metadata (zarr_version=3) |
| B1-open | B | pass | 8.0/8 | requests_to_open=2, bytes_to_open=4096; metadata_walk objects_visited=6, complete=True |
| B2-crs | B | skipped | 1.5/3 | no machine-readable CRS found; live probe unavailable to confirm further |
| B3-semantics | B | pass | 5.0/5 | conventions_attr=True; units_and_name=True |
| C1-interactive | C | fail | 2.0/10 | chunks average 118.4 MB per sampled chunk, far above the 1-4 MB interactive band |
| C5-codec | C | partial | 3.0/5 | gzip: works, but zstd/blosc dominate gzip/zlib/deflate on every axis |
| D1-range | D | pass | 8.0/8 | HTTP Range (206) confirmed via live probe |
| E1-etag | E | pass | 5.0/5 | stable ETag observed across two HEAD probes |

## Smoke-test telemetry

| Status | Requests-to-open | Bytes-to-open | Time-to-open | Subset TTFB | Throughput | Total bytes |
|---|---|---|---|---|---|---|
| pass | 2 | 4096 | 0.18 s | 0.09 s | 42.3 MB/s | 26214400 |

## Compression

### Codec inspection

| Variable | Codec | Status | Note |
|---|---|---|---|
| sea_water_temperature | gzip | warn | gzip: works, but zstd/blosc dominate gzip/zlib/deflate on every axis |

### Benchmark (measured on one sampled chunk)

Grid per read-heavy-archive conventions (zstd-1/3/5, blosc-lz4, blosc-zstd-3, ± byte shuffle); judge with the transfer model `TTFB + compressed/network_bw + uncompressed/decompress_speed` — over egress, ratio wins; in-region, decode speed can dominate.

| Config | Ratio | Compress MB/s | Decompress MB/s |
|---|---|---|---|
| **current (gzip) (current)** | 2.1x | 80.0 | 210.0 |
| zstd-1 | 2.4x | 350.0 | 900.0 |
| zstd-3+shuffle | 2.9x | 210.0 | 850.0 |

## Chunking recommendations (all three profiles)

> Within-chunk partial reads are impossible by design — the chunk is the
> atomic read unit — so oversized chunks tax every partial read and
> undersized chunks tax every bulk read.

| Profile | Current (per sampled asset) | Recommended | Expected requests: canonical pattern |
|---|---|---|---|
| Interactive / visualization | zarr: ❌ chunks average 118.4 MB per sampled chunk, far above the 1-4 MB interactive band | ~1-4 MB chunks/tiles + overviews | one viewport tile: 1 |
| AI training (throughput) | — | 10-100 MB (sweet spot 32-64 MB) chunks/shards, shape aligned with sampling | one ~64 MB batch: 1-2 |
| AI agentic | — | 1-16 MB chunks; schema in ONE request; stable HTTPS URLs | catalog -> variables -> subset: <=3 |

Primary intended profile (user-stated): **training**.

## Prioritized remediation

1. **[C1-interactive, 10 pts at stake]** rewrite with ~2 MB chunks/tiles aligned to viewport access (e.g. 1x180x360), or publish a lower-resolution overview variable for interactive use
2. **[C5-codec, 5 pts at stake]** recompress with zstd level 3 plus byte shuffle (faster decode at a similar ratio)

## Sample frame (reproducibility)

```json
{
  "chunk_index": [
    0,
    3,
    3
  ],
  "seed": 42,
  "variable": "sea_water_temperature"
}
```

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
