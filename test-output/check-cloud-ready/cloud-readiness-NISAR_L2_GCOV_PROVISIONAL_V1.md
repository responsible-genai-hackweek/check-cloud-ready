# Cloud readiness: NISAR L2 GCOV (PROVISIONAL V1), granule NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001

**Verdict: NOT READY — hosting, range access, and metadata conventions are all in good shape, but the 512×512 HDF5 chunks compress to well under 1 MB (median sampled 4 KB, ≤0.64 MB even on dense interior data), which is pathological for cloud access and a FAIL every remote consumer will hit; full-product metadata is also badly dispersed (1,631 range requests to walk the tree).**

**Entrypoint:** https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001/NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5 · **Format:** HDF5 (dimension-scale structured; no `_NCProperties`) · **Assessed:** 2026-08-26 · Variables: `/science/LSAR/GCOV/grids/frequencyA/{HHHH, HVHV}` (primary geophysical covariance terms; frequencyB counterparts and coordinate arrays also measured)

| # | Criterion | Result | Evidence |
|---|-----------|--------|----------|
| 1 | On object storage        | PASS | Backed by S3 `sds-n-cumulus-prod-nisar-products` (us-west-2), served via CloudFront + Earthdata TEA; EDL redirect detected and `.netrc` auth worked. Direct in-region S3 access model also exists (ASF temporary credentials); restricted-egress S3 is a hosting model, not a failure. |
| 2 | Random access (ranges)   | PASS | `Range: bytes=0-1023` → 206 Partial Content, `Content-Range: bytes 0-1023/3510632448`; file opens in 3 range requests. TTFB ~2.6 s through the EDL/CloudFront redirect chain from outside AWS. |
| 3a| Chunk size               | FAIL | 512×512 float32 chunks = 1.05 MB uncompressed; sampled compressed median 0.004 MB (range 0.004–0.61 MB); dense interior chunks ~0.62–0.64 MB. All below the ~1 MB floor (target 8–16 MB). 4,692 chunks per frequencyA variable. |
| 3b| Chunk shape for use cases| not assessed | No target access patterns stated (unattended run). Note: for a single-epoch 2-D granule, square spatial chunks are shape-neutral; the problem is size, not shape. |
| 4a| CF conventions           | PASS | `Conventions = CF-1.7`; `grid_mapping` variable `projection` with CF grid-mapping attrs + `spatial_ref` WKT + `epsg_code` 32610; coordinates carry `standard_name` (`projection_x/y_coordinate`) and units; data vars have `long_name`, `units`, `_FillValue`. `xarray.open_dataset(engine="h5netcdf", group=..., phony_dims="sort")` opens the grids group cleanly. Minor: no CF `standard_name` exists for covariance terms (acceptable — `long_name` present); coordinate `units` is "meters" rather than canonical "m". |
| 4b| GeoZarr (Zarr only)      | not assessed | Not a Zarr store. |
| — | Compression              | WARN | Current: gzip-1 + shuffle, ~1.65× on dense data. zstd-3+shuffle matches ratio (1.69×) with ~1.8–2× faster decode; blosc-zstd-3+shuffle decodes ~7× faster (multithreaded) at equal ratio — triggers the ">2× decode at equal ratio" WARN. gzip is, however, the maximally compatible HDF5 filter; the practical route to better codecs is a cloud-optimized copy. |
| — | Usage discoverability    | noted — strong | NISAR Data User Guide (nisar-docs.asf.alaska.edu, incl. a GCOV page with the exact `/science/LSAR/GCOV/grids/frequencyA/HHHH` path), MAAP docs with end-to-end fsspec+xarray/h5netcdf code, NASA Earthdata catalog entry with DOI (10.5067/NIL2GCOV-P1), ASF workshop notebooks, ArcGIS tutorials. Applications (biomass, soil moisture, inundation/flood, crop mapping, disturbance) are well documented. An agent would not have to guess. |

## Findings

### 3a — Chunk size (FAIL)
`HHHH` and `HVHV` (34,776 × 35,280 float32) are chunked 512×512 with gzip-1+shuffle: 1.05 MB uncompressed, and measured compressed sizes never reach 1 MB — 4 KB median across a random sample (many chunks are pure fill/NaN outside the swath) and ~0.62 MB for fully dense interior data. Cloud reads pay a fixed per-request cost; at these sizes the request overhead dominates transfer. Reading one full variable means up to 4,692 GETs; even a modest 5,000 × 5,000-pixel regional subset touches ~100 chunks. A measured 512×512 subset read cost 8 requests and 2.57 MB transferred for 1 MB of data. Fill-value chunks are also *stored* (compressed to ~4 KB) rather than elided, inflating request counts over nodata regions.

### Metadata dispersal (finding under criteria 2/3a)
Opening the file is cheap (3 requests), and navigating to `frequencyA` + reading coordinates costs ~43 requests / ~4 s. But the product tree holds 343 objects whose metadata is scattered through the file: a full metadata walk (what a naive `xr.open_datatree` or whole-product tool does) took **1,631 range requests and ~137 s** to read only 2.6 MB. HDF5 paged aggregation (`h5repack -S PAGE -G <pagesize>`) or a kerchunk/VirtualiZarr sidecar would collapse this to O(1) requests.

### 3b — Chunk shape (not assessed)
No user-stated access patterns. Qualitatively: each granule is a single-epoch 2-D scene, so square spatial chunks are a reasonable neutral shape; time-series use cases operate across granules (a stacking/virtualization concern, not an in-file chunk-shape concern).

### Compression (WARN)
gzip-1+shuffle achieves ~1.65× on dense backscatter; the benchmark grid shows zstd-3+shuffle at the same ratio with ~2× decode speed and blosc-zstd-3+shuffle ~7× (multithreaded) — but no shortlist codec beats gzip on *ratio* by more than a few percent, so the win is decode throughput, not bytes on the wire. Because non-gzip HDF5 filters hurt ecosystem compatibility, codec change only makes sense as part of a cloud-optimized copy (Zarr), not an in-place edit. Informational lossy data point (a provider decision, not a recommendation): bit-rounding to 10 keep-bits + zstd+shuffle reached ~7.4–7.7× (vs ~5× lossless on the same NaN-containing sample) — worth evaluating against NISAR's radiometric accuracy budget if egress volume matters.

### CF conventions (PASS, minor notes)
Structure is genuinely good: CF-1.7 declared, complete grid-mapping (WKT + EPSG + CF parameters), dimension scales attached, statistics attributes (`min/max/mean/stddev`) precomputed, `_FillValue` = NaN consistent with float32. Minor polish: `units: "meters"` → `"m"`; no `_NCProperties` (file written with h5py rather than netCDF-C — cosmetic, but adding it makes format detection and some tooling friendlier).

## Recommendations for the data provider

1. **Rechunk the covariance grids from 512×512 to ~2048×2048** (with the existing shuffle+deflate or zstd). Measured: a dense 2048×2048 float32 block compresses to ~10 MB — squarely inside the 8–16 MB target — and cuts chunks-per-variable from 4,692 to ~300. This single change flips the FAIL.
2. **Fix metadata dispersal**: write products with HDF5 paged aggregation (`H5Pset_file_space_strategy` PAGE / `h5repack -S PAGE -G 8388608`) so open-and-inventory costs O(1) requests instead of 1,631.
3. **Publish a cloud-optimized companion** — either a kerchunk/VirtualiZarr reference sidecar per granule (zero data duplication; consolidates metadata and exposes chunks directly; note it cannot fix the too-small native chunks, so pair with #1) or a Zarr copy with zstd-3+shuffle (equal ratio to gzip, ~2–7× faster decode).
4. **Don't store fill-only chunks** (or use the rechunked layout plus `H5Pset_fill_time(H5D_FILL_TIME_NEVER)`-style elision in the Zarr copy): a large fraction of sampled chunks are pure nodata compressed to ~4 KB each and still cost one GET apiece.
5. **Evaluate lossy precision control** (xbitinfo keep-bits analysis) against the L2 radiometric accuracy requirement — measured ~1.5× additional size reduction at keepbits=10; this is a science-team decision to make explicitly, not a default.
6. Minor CF polish: `units: "m"` on coordinates; add `_NCProperties` or write via netCDF-C so the files self-identify as netCDF-4.
