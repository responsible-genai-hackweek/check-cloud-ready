---
name: earth-science-cloud-readiness
description: >-
  Assess whether an Earth-systems dataset is cloud-optimized / cloud-native
  ready, recommend chunking for AI and AI-agentic workflows, run live smoke
  tests against the data, and generate a graded assessment report
  (A–F tier, 0–100 score). Use this skill whenever the user mentions
  cloud-optimized, cloud-native, COG, Cloud-Optimized GeoTIFF, Zarr, GeoZarr,
  Icechunk, kerchunk, VirtualiZarr, ARCO, analysis-ready cloud-optimized data,
  STAC assessment or evaluating a STAC catalog/collection, GeoParquet, COPC,
  PMTiles, FlatGeobuf, cloud-optimized HDF5/NetCDF, HDF4, Croissant or GeoCroissant
  dataset descriptors, chunking recommendations, compression or codec
  evaluation for array data, "is this dataset AI-ready",
  "is this data cloud-ready", grading or auditing a data catalog, or asks
  whether data on S3/GCS/Azure is efficiently accessible — even if they don't
  use the word "assessment". Scope is cloud + data only (storage layout,
  formats, metadata, access); never compute, orchestration, models, or cost.
---

# Earth Science Cloud Readiness Assessment

Grade Earth-systems datasets on cloud-readiness and AI-workflow fit, using
live network probes plus a published-checklist rubric, and produce
`assessment-report.md` + machine-readable `findings.json`.

**Scope guard:** assess storage layout, formats, metadata, and access
patterns ONLY. Politely decline to assess compute, orchestration, model
architectures, or cost — even if asked in the same breath.

## Workflow

1. **Detect the input type** (see table below) and confirm your reading with
   the user only if genuinely ambiguous.
2. **Elicit intent (one short exchange, skip what's already known).** These
   datasets are hierarchical with obscure variable names, so before assessing
   ask the user — unless the conversation already answers it:
   - *Which variables/bands/columns matter?* (or "all", or "you suggest").
     If they defer, propose candidates from the dataset's own metadata and
     documentation — search the catalog/landing page first, then general
     knowledge — and say which you'll sample. Pass choices via
     `--variables t2m,precip`.
   - *What access pattern should be optimized?* Map their answer to
     `--profile interactive|training|agentic`. No answer → assess all three
     profiles (the report always shows all three regardless; `--profile`
     only emphasizes one in the narrative).
   Don't interrogate; one message with both questions, defaults offered.
3. **Read the rubric**: `references/rubric.md` (generic scoring engine,
   dimensions A–E, tiers). Always read this before scoring anything.
4. **Read the per-format reference(s)** for the formats you actually
   encounter (don't read all of them):
   - COG / GeoTIFF → `references/cog.md`
   - Zarr v2/v3, virtual Zarr (kerchunk/VirtualiZarr/Icechunk), GeoZarr → `references/zarr.md`
   - HDF5 / NetCDF-4 → `references/hdf5-netcdf.md`
   - GeoParquet / Parquet → `references/geoparquet.md`
   - FlatGeobuf, COPC/LAS/LAZ, PMTiles → `references/vector-pointcloud-tiles.md`
   - NetCDF-3, HDF4, GRIB2, shapefile, CSV, zip/tar → `references/legacy.md`
   - Chunking targets & sizing math (always, for Dimension C) → `references/chunking-for-ai.md`
   - Interpreting compression trials / writing C5 remediations → `references/compression.md`
   - Croissant/GeoCroissant input or `--emit-croissant` → `references/croissant.md`
5. **Run the assessment**:
   ```bash
   python scripts/assess.py <INPUT> --workdir ./assessment \
       [--variables v1,v2] [--profile interactive|training|agentic] \
       [--max-assets N] [--all] [--emit-croissant] [--no-network]
   ```
   `assess.py` detects the input, crawls/samples, dispatches format
   assessors, invokes `smoke_test.py` per sampled asset, scores against the
   rubric, and writes `findings.json`.
6. **Render the report**:
   ```bash
   python scripts/report.py ./assessment/findings.json \
       --template assets/report-template.md -o ./assessment/assessment-report.md
   ```
7. **Review and hand over**: read the generated report, verify every failed
   check has a remediation, verify the three-profile chunking table is
   present (mandatory even for A-tier), then present both files to the user
   with a one-paragraph verdict in the conversation.

## Input detection

| Input looks like | Treat as | Handling |
|---|---|---|
| URL ending `/stac/v1`, has `/search` or conforms to STAC API | STAC API | `pystac-client` search; sample N per collection |
| `catalog.json` / `collection.json` / `item.json` (URL or path) | Static STAC | walk `child`/`item` links with `pystac`; no API assumptions |
| `https://`, `s3://`, `gs://`, `az://` file or store root | Direct asset | detect format, assess directly |
| Local file or directory | Uploaded data | detect format(s), assess directly |
| JSON-LD with `@context` mentioning `mlcommons.org/croissant` | Croissant / GeoCroissant | resolve `distribution` FileObject/FileSet to URLs; see `references/croissant.md` |

Format detection order: **file extension → STAC asset media type → magic
bytes via a ranged read of the first bytes**. Never download whole files.

## Sampling strategy (catalogs)

Default: **N=3 representative assets per collection** — first, middle-ish,
and last by item datetime — **grouped by media type** (so a collection with
COGs and NetCDF gets 3 of each). Record the exact sample frame (item IDs,
asset keys, selection rule) in findings so the run is reproducible.
Overrides: `--all` (every item) and `--max-assets N`.

Mixed catalogs: grade **per media type** and report the distribution of
tiers, never just an average.

## Rubric summary (full tables in `references/rubric.md`)

| Dim | What | Pts |
|---|---|---|
| A | Format & structure — is the container inherently cloud-friendly? | 30 |
| B | Metadata locality & richness — requests-to-open, CF/CRS/nodata, STAC quality | 20 |
| C | Chunking & AI-workflow fit — layout vs. the three access profiles | 25 |
| D | Access & transport — live-verified Range support, auth, CORS, HTTPS | 15 |
| E | Reproducibility & governance — versioning, checksums, license, DOI | 10 |

**Tiers:** 90–100 Cloud-Native (A) · 75–89 Cloud-Optimized (B) ·
55–74 Cloud-Friendly with gaps (C) · 35–54 Cloud-Hosted only (D) ·
<35 Not cloud-ready (F).

Hard rules:
- A dataset **cannot score above C-tier if the smoke test FAILS**,
  regardless of static checks. (SKIPPED ≠ FAILED — see below.)
- "Cloud-hosted" ≠ "cloud-optimized": being on S3 earns **zero** points by
  itself. Say this explicitly in the verdict when applicable.
- Every deduction must be traceable to evidence recorded in `findings.json`
  (header bytes, request counts, timings).
- Every failed check must map to ≥1 concrete remediation (exact commands
  where possible: `gdal_translate -of COG`, `rechunker`, `kerchunk` /
  `VirtualiZarr` snippets, `h5repack -S PAGE -G <pagesize>`).
- The report always ends with the three-profile chunking recommendation
  table (interactive / AI training / AI agentic), even for A-tier data.

## Smoke test (mandatory, bounded)

`scripts/smoke_test.py` runs per sampled asset with hard caps of
**~25 MB total transfer and 60 s per asset**:

1. HEAD: status, `Content-Length`, `Accept-Ranges`, `ETag`, TLS.
2. Ranged reads: first 16 KB + one interior range; verify HTTP 206 and byte
   counts.
3. Instrumented lazy open (rasterio / zarr / h5py+fsspec / pyarrow),
   counting HTTP **requests-to-open** and **bytes-to-open**, plus
   time-to-open.
4. Tiny subset read (one chunk / tile / row group): TTFB and throughput.
   The variable is user-steered when `--variables` was given.
5. **Empirical compression trials** on the decoded chunk (local CPU, zero
   network cost): grid of zstd-1/3/5, blosc-lz4, blosc-zstd-3, ± byte
   shuffle — ratio + compress/decompress throughput, feeding the C5 finding
   (interpretation: `references/compression.md`).
6. Format validation: `rio cogeo validate` (COG), consolidated-metadata +
   one-chunk decode (Zarr), h5py chunk/page introspection (HDF5), footer +
   row-group stats (Parquet).
7. **Graceful degradation**: network blocked or auth failure ⇒ mark smoke
   test `SKIPPED` (not failed), state why, and downgrade the report's
   **confidence label** — not the score.

Required libraries (degrade feature-by-feature if missing, never crash):
`rasterio`, `rio-cogeo`, `zarr`, `xarray`, `h5py`, `fsspec`, `s3fs`,
`pyarrow`, `pystac`, `pystac-client`, `httpx`.

## Outputs

- `findings.json` — machine-readable: sample frame, per-asset evidence and
  scores, smoke-test telemetry, rollups. Schema stable for dashboards.
- `assessment-report.md` — rendered from `assets/report-template.md`:
  executive summary + tier, scorecard, per-asset findings, smoke telemetry
  table, three-profile chunking table (current vs recommended, expected
  request counts), prioritized remediation list, sources & rubric
  provenance (the published checklists cited in `references/rubric.md`).
- Optional `croissant.jsonld` via `--emit-croissant` — a GeoCroissant record
  with verified distributions and namespaced `cloudReadiness:*` properties
  (see `references/croissant.md`).
