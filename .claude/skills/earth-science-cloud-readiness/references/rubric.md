# Cloud-Readiness Rubric (Generic Engine)

This file is the **domain-agnostic** scoring core. It must contain no
Earth-science-specific logic; format specifics live in the per-format
reference files, which supply *checks* that map onto the dimensions below.

## Contents
1. Scoring model
2. Dimension A — Format & structure (30 pts)
3. Dimension B — Metadata locality & richness (20 pts)
4. Dimension C — Chunking & AI-workflow fit (25 pts)
5. Dimension D — Access & transport (15 pts)
6. Dimension E — Reproducibility & governance (10 pts)
7. Tiers, caps, and rollup rules
8. Evidence & traceability requirements
9. Sources / rubric provenance (cite these in every report)

---

## 1. Scoring model

- Each **asset** gets a 0–100 score = A + B + C + D + E.
- Each dimension is scored from named **checks**. Each check records:
  `{id, dimension, points_possible, points_awarded, status
  (pass|partial|fail|skipped|n/a), evidence, remediation}`.
- `skipped` checks (e.g., network blocked) redistribute nothing — they are
  scored at their **static-inference value** where possible, otherwise
  excluded and the dimension re-normalized; the report's *confidence label*
  is downgraded (High → Reduced → Low), never the score itself.
- `n/a` checks (e.g., "overviews present" for a vector file) are excluded
  and the dimension re-normalized.

## 2. Dimension A — Format & structure (30 pts)

Is the container inherently cloud-friendly?

| Class | Baseline | Examples |
|---|---|---|
| Cloud-native | 26–30 | Zarr v2/v3, Icechunk, COG, GeoParquet, COPC, FlatGeobuf, PMTiles |
| Cloud-optimizable AND optimized | 18–25 | HDF5/NetCDF-4 with paged aggregation / sane chunks / consolidated-ish metadata, or fronted by kerchunk/VirtualiZarr index |
| Cloud-optimizable, NOT optimized | 8–17 | vanilla NetCDF-4/HDF5 with tiny chunks, scattered metadata |
| Cloud-hostile | 0–7 | NetCDF-3, GRIB2 without index, shapefile, zipped/tarred anything, CSV dumps |

Baseline is then adjusted by format-specific checks (see the per-format
reference file), e.g. COG internal tiling/overviews/IFD placement, Zarr
consolidated metadata/sharding, HDF5 chunk size/page aggregation. A
cloud-native container with broken internals (COG failing `rio cogeo
validate`) drops out of its class baseline. **Cloud-hostile formats always
receive a mandatory migration recommendation** (see `legacy.md`).

## 3. Dimension B — Metadata locality & richness (20 pts)

| Check | Pts | Pass criteria |
|---|---|---|
| B1 One-or-few-requests-to-open | 8 | Lazy open completes in a small, bounded number of HTTP requests. Full: ≤3 requests. Partial: ≤10. Fail: >10 or unbounded per-array walks / metadata scavenger hunts. Measured live when possible; inferred statically otherwise (consolidated Zarr metadata, COG header at front, Parquet footer ⇒ pass). |
| B2 CRS machine-readable | 3 | CRS in-file AND/OR catalog `proj:` extension. |
| B3 Semantics & conventions | 5 | Domain conventions honored (for Earth gridded data: CF conventions), units present, nodata/fill declared, scale/offset declared where used, names interpretable without a sidecar PDF. |
| B4 Catalog quality | 4 | Catalog entries (e.g., STAC items) have correct media types, asset roles, relevant extensions (`raster:`, `proj:`, `datacube:`), checksums, license. n/a when no catalog exists (renormalize). |

## 4. Dimension C — Chunking & AI-workflow fit (25 pts)

Score the *actual* chunk/tile/shard layout against three access profiles
(targets and sizing math in `chunking-for-ai.md`). Print the
recommendation table for **all three profiles in every report**, even when
the current layout scores full marks.

| Check | Pts | Criteria |
|---|---|---|
| C1 Interactive/visualization fit | 5 | ~1–4 MB compressed chunks/tiles; overviews/pyramids mandatory for rasters; latency-dominated reads feasible. |
| C2 AI-training fit | 7 | ~10–100 MB (sweet spot 32–64 MB) compressed chunks or shards; chunk shape aligned with likely sampling pattern (spatial patches ⇒ chunk small in time, larger in x/y; time-series ⇒ inverse). Penalize orthogonal layouts (one-timestep-per-chunk global fields when per-pixel time series is the use case). |
| C3 AI-agentic fit | 7 | ~1–16 MB chunks; schema enumerable in ONE request (consolidated metadata); stable HTTPS URLs (not signed URLs that expire mid-session); self-describing names/units. Explicit sub-check: **catalog → variable list → one subset read achievable in ≤3 requests?** |
| C4 Chunk-count sanity | 3 | Total object count manageable (millions of tiny objects = listing/management failure ⇒ recommend Zarr v3 sharding). |
| C5 Codec | 3 | Read-favoring codecs (ZSTD, LZ4; DEFLATE acceptable, but gzip/zlib is a remediation target since zstd dominates it) vs slow high-ratio codecs. Judged against the smoke test's **empirical codec trials** on a sampled chunk (`compression_trials`) when available — quote measured ratio/decode speed in evidence. Lossy (WebP/JPEG) flagged unless a visualization product. Interpretation guide: `references/compression.md`. |

Note in every report: within-chunk partial reads are impossible by design —
the chunk is the atomic read unit — so oversized chunks directly tax every
partial read. This is why the profiles bracket sizes rather than "bigger is
better."

## 5. Dimension D — Access & transport (15 pts)

**Verified live, not from metadata.** If network unavailable ⇒ `skipped`,
confidence downgraded.

| Check | Pts | Pass criteria |
|---|---|---|
| D1 Range requests | 6 | Real Range request answered with 206 + correct byte count; `Accept-Ranges: bytes`. |
| D2 Auth & transparency | 3 | Anonymous access, or auth clearly documented; requester-pays flagged. NASA Earthdata in-region-only S3 whose `/s3credentials` endpoint is documented in CMR (resolvable from a granule ID) counts as "auth clearly documented" — the out-of-region 403 is expected, not a transparency failure. |
| D3 HTTPS & CORS | 3 | HTTPS with valid TLS; CORS headers if browser use is plausible. |
| D4 Clean data path | 3 | No redirect chains, HTML interstitials, or click-through pages in front of data URLs; region/endpoint documented. |

## 6. Dimension E — Reproducibility & governance (10 pts)

| Check | Pts | Pass criteria |
|---|---|---|
| E1 Versioning | 2 | Icechunk snapshots, STAC item versions, or at minimum stable ETags. |
| E2 Checksums | 2 | Published checksums or strong ETags. |
| E3 License | 2 | Machine-readable license. |
| E4 Citation | 1 | DOI or citation guidance (STAC scientific extension, `cite-as`). |
| E5 Contact | 1 | Maintainer/contact identifiable (providers/contacts). |
| E6 Usage discoverability | 1 | Could an agent find *how to use* this data without guessing — example/tutorial/documentation links (rel=describedby/example/about, notebook links) discoverable from the catalog entry? Skipped (0.5) when input is a bare asset with no catalog; the skip itself is a finding worth narrating. |
| E7 Applications discoverability | 1 | Could an agent match this dataset to a use case — ≥3 keywords or a substantive (≥200-char) description in the catalog? This determines whether agents can *discover* the dataset when a user arrives with an application in mind, not just assess it once found. |

## 7. Tiers, caps, and rollup rules

| Score | Tier |
|---|---|
| 90–100 | **A — Cloud-Native** |
| 75–89 | **B — Cloud-Optimized** |
| 55–74 | **C — Cloud-Friendly with gaps** |
| 35–54 | **D — Cloud-Hosted only** |
| <35 | **F — Not cloud-ready** |

Hard rules:
- **Smoke-test FAIL caps the score at 74 (C-tier max)** regardless of
  static checks. SKIPPED does not cap; it downgrades confidence.
- Being on object storage earns **zero points by itself**. Verdict language
  must distinguish "cloud-hosted" from "cloud-optimized" whenever the gap
  applies.
- Rollup: dataset/collection score = per-media-type medians, reported per
  media type with the distribution. Never report only a blended average for
  mixed catalogs. Collection tier = tier of the **dominant media type by
  asset count**, with other types listed alongside.

## 8. Evidence & traceability

Every deduction in `findings.json` must carry evidence: header bytes
inspected, HTTP request counts, response headers, byte counts, timings, or
the metadata field that failed. Every `fail`/`partial` check must carry at
least one remediation with an exact command where one exists.

## 9. Sources / rubric provenance

Cite these in the report's provenance section:

- **Cloud-Optimized Geospatial Formats Guide** (guide.cloudnativegeo.org) —
  COG, Zarr, and cloud-optimized HDF5/NetCDF checklists.
- **rio-cogeo** validation rules for COG.
- **NASA ESDIS / IMPACT** cloud-optimization guidance for HDF5 — paged
  aggregation, ~8 MB page-size findings, avoiding metadata scattered
  through the file.
- **Pangeo community** chunking guidance for analysis-ready,
  cloud-optimized (ARCO) data.
- **Zarr v3 specification** — sharding codec, consolidated-metadata
  behavior.
- **STAC best practices** — asset roles, projection/raster extensions.
- **Datacube guide** (developmentseed.org/datacube-guide) — chunking
  worst-practices catalog.
- **Cross-provider request-size guidance** — AWS S3 performance
  guidelines/byte-range whitepaper (8–16 MB ranged GETs), Azure Blob
  performance checklist (≥4 MiB blocks), GCS best practices, ESIP cloud
  optimization practices: basis for the ~8–16 MB compressed wire target.
- **HEFTIE Zarr benchmarks** and Klöwer et al. bit-rounding (xbitinfo) —
  compression grid conventions and precision-filter guidance
  (`references/compression.md`).
