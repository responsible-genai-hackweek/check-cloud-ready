# Cloud Readiness Assessment: https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001/NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5

**Assessed:** 2026-08-26T22:47:24.555207+00:00 · **Input:** `https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001/NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5` (asset) ·
**Confidence:** high

## Executive summary

**Tier: D — Cloud-Hosted only · Score: 52.2/100**

The dataset grades **D (Cloud-Hosted only)** at 52.2/100 (confidence: high), assessed on 1 sampled asset(s), dominant format **hdf5**. Note the distinction: this data is **cloud-hosted, not cloud-optimized** — it sits on reachable object storage (which by itself earns zero points), but its internal layout fights ranged access. The remediation plan below closes that gap. Biggest gaps: C2-training (estimated compressed chunk ≈ 0.52 MB); B3-semantics (units/nodata/CF not confirmed).

## Scorecard

| Dimension | What | Score | Max |
|---|---|---|---|
| A | Format & structure | 10.0 | 30 |
| B | Metadata locality & richness | 11.2 | 20 |
| C | Chunking & AI-workflow fit | 13.0 | 25 |
| D | Access & transport | 14.0 | 15 |
| E | Reproducibility & governance | 4.0 | 10 |
| **Total** | | **52.2** | **100** |



## Per-asset findings

### `NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5` — hdf5 — 52.2/100 (D)
URL: `https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001/NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5`

| Check | Dim | Status | Pts | Evidence |
|---|---|---|---|---|
| A-class | A | partial | 10/30 | format=hdf5, class=cloud-optimizable; chunks < 1 MB in: ['science/LSAR/GCOV/grids/frequencyA/inputDataExceptionMask', 'science/LSAR/GCOV/grids/frequencyA/mask', 'science/LSAR/GCOV/ |
| B1-open | B | pass | 8/8 | requests_to_open=1, bytes_to_open=0 |
| B2-crs | B | fail | 0/3 | no machine-readable CRS found |
| B3-semantics | B | fail | 1/5 | units/nodata/CF not confirmed |
| B4-catalog | B | n/a | 0/4 | no catalog in input |
| C1-interactive | C | partial | 2.0/5 | estimated compressed chunk ≈ 0.52 MB |
| C2-training | C | fail | 0/7 | estimated compressed chunk ≈ 0.52 MB |
| C3-agentic | C | pass | 7/7 | catalog→variables→one subset ≈ 2 requests; estimated compressed chunk ≈ 0.52 MB |
| C4-count | C | pass | 3/3 | estimated total chunks ≈ 33249 |
| C5-codec | C | fail | 1/3 | no compression observed; measured on one sampled chunk: best zstd-3+shuffle ratio 20971.52x, decode 17143.1 MB/s (10 grid cells trialed) |
| D1-range | D | pass | 6/6 | [{"range": "0-16383", "http_status": 206, "bytes": 16384, "expected": 16384, "ok": true, "latency_ms": 560.1}, {"range": "1755316224-1755332607", "http_status": 206, "bytes": 16384 |
| D2-auth | D | pass | 3/3 | HTTP 200 |
| D3-https-cors | D | partial | 2/3 | tls=True, CORS Allow-Origin=None |
| D4-cleanpath | D | pass | 3/3 | redirects=1, content_type=binary/octet-stream |
| E1-version | E | partial | 1/2 | etag="bfc3d4b0f8c6237d59cce9e7b4086269-14", icechunk=False |
| E2-checksums | E | partial | 1/2 | sha256=none, strong_etag=True |
| E3-license | E | fail | 0/2 | license=None |
| E4-citation | E | skipped | 0.5/1 | DOI/citation not verifiable from asset alone |
| E5-contact | E | skipped | 0.5/1 | contact not verifiable from asset alone |
| E6-usage | E | skipped | 0.5/1 | no catalog in input; an agent would have to research or guess how to access this data |
| E7-applications | E | skipped | 0.5/1 | no catalog in input; use cases not discoverable |


## Smoke-test telemetry

| Asset | Status | Requests-to-open | Bytes-to-open | Time-to-open | Subset TTFB | Throughput | Total bytes |
|---|---|---|---|---|---|---|---|
| NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5 | pass | 1 | 0 | 1.419 s | 0.897 s | 1.17 MB/s | 32768 |



## Compression trials (measured on one sampled chunk per asset)

Grid per read-heavy-archive conventions (zstd-1/3/5, blosc-lz4, blosc-zstd-3, ± byte shuffle); judge with the transfer model `TTFB + compressed/network_bw + uncompressed/decompress_speed` — over egress, ratio wins; in-region, decode speed can dominate. For floats, precision filters (bit rounding via xbitinfo) beat any codec change; see `references/compression.md`.

| Asset | Codec | Ratio | Compress MB/s | Decompress MB/s |
|---|---|---|---|---|
| NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5 | zstd-3+shuffle | 20971.52x | 5608.6 | 17143.1 |
| NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5 | zstd-5+shuffle | 20971.52x | 3466.4 | 17248.9 |
| NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5 | zstd-1+shuffle | 20560.31x | 6241.5 | 16320.2 |
| NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5 | zstd-3 | 9709.04x | 2155.9 | 2945.8 |
| NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5 | zstd-5 | 9709.04x | 1002.5 | 1347.1 |


## Chunking recommendations (all three profiles)

> Within-chunk partial reads are impossible by design — the chunk is the
> atomic read unit — so oversized chunks tax every partial read and
> undersized chunks tax every bulk read.

| Profile | Current (per sampled asset) | Recommended | Expected requests: canonical pattern |
|---|---|---|---|
| Interactive / visualization | hdf5: ⚠️ estimated compressed chunk ≈ 0.52 MB | ~1–4 MB chunks/tiles + overviews | one viewport tile: 1 |
| AI training (throughput) | hdf5: ❌ estimated compressed chunk ≈ 0.52 MB | 10–100 MB (sweet spot 32–64 MB) chunks/shards, shape aligned with sampling | one ~64 MB batch: 1–2 |
| AI agentic | hdf5: ✅ catalog→variables→one subset ≈ 2 requests; estimated compressed chunk ≈ 0.52 MB | keep as-is (1–16 MB chunks; schema in ONE request; stable HTTPS URLs) | catalog → variables → subset: ≤3 |

No primary profile was stated; scores assume the most plausible profile per format and the table above covers all three.

## Prioritized remediation

1. **[A-class, 30 pts at stake]** h5repack -l /var:CHUNK=1x512x512 in.h5 out.h5 _(e.g., asset `NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5`)_
2. **[C2-training, 7 pts at stake]** rechunk to 32-64 MB chunks/shards aligned with sampling pattern (rechunker recipe in references/zarr.md) _(e.g., asset `NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5`)_
3. **[B3-semantics, 5 pts at stake]** declare CF attrs, units, nodata/_FillValue, scale/offset _(e.g., asset `NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5`)_
4. **[C1-interactive, 5 pts at stake]** target ~1-4 MB tiles/chunks + overviews _(e.g., asset `NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5`)_
5. **[B2-crs, 3 pts at stake]** embed CRS in-file and add STAC proj: extension _(e.g., asset `NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5`)_
6. **[C5-codec, 3 pts at stake]** data is compressible: apply zstd level 3 + shuffle (measured 20971.52x on sampled chunk) _(e.g., asset `NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5`)_
7. **[D3-https-cors, 3 pts at stake]** serve over HTTPS; add CORS headers if browser use is plausible _(e.g., asset `NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5`)_
8. **[E3-license, 2 pts at stake]** add a machine-readable license _(e.g., asset `NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5`)_

## Sample frame (reproducibility)

```json
{
  "strategy": "single asset",
  "selection_rule": "as given"
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

Full rubric: `references/rubric.md` in the `earth-science-cloud-readiness`
skill. Machine-readable evidence: `findings.json` alongside this report.
