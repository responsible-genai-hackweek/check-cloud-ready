---
name: assess-cloud-readiness
description: >-
  Use when the user asks whether an Earth-systems dataset is cloud ready,
  cloud optimized, cloud native, analysis ready (ARCO), or AI-ready; runs
  check-cloud-ready; asks to assess or grade chunking, compression, or codec
  choices for a NetCDF, HDF4/HDF5, Zarr, GeoZarr, Icechunk,
  kerchunk/VirtualiZarr, COG, GeoParquet, COPC, PMTiles, or FlatGeobuf
  dataset; asks to audit a STAC catalog/collection or a Croissant/GeoCroissant
  record; or provides a dataset URL (s3://, gs://, az://, https://, or a
  store root) and wants to know how it will perform for cloud access — even
  if they never say "assessment". Scope is storage layout, formats, metadata,
  and access only; never compute, orchestration, models, or cost.
---

# Assess Cloud Readiness

Grade an Earth-systems dataset's cloud readiness with live network probes
plus a published-checklist rubric, and produce a report whose **first line
is a categorical verdict a data provider can act on**, backed by a numeric
score, per-check evidence, and machine-readable `findings.json`.

The audience is **data providers who can change file structure and embedded
metadata** — every non-pass finding must end in a remediation they can
implement (exact commands where possible), not just a grade.

**Scope guard:** assess storage layout, formats, metadata, and access ONLY.
Politely decline compute, orchestration, model, or cost questions — even
when asked in the same breath.

## Ground rules

- **Never ask the user to type credentials into the conversation.**
  Earthdata Login (EDL) and S3 credentials come from `~/.netrc` or
  environment variables — see `references/access-and-auth.md`. If
  credentials are needed and absent, pause, say exactly what to set up,
  and continue once confirmed.
- **Auth must be proven at GET, not HEAD.** Distribution layers (e.g. NASA
  TEA) answer HEAD anonymously but gate GET behind a redirect flow; a
  200-HEAD is not evidence of anonymous access. All scripts share one
  netrc-aware authenticated session (see `references/access-and-auth.md`).
- **Measure, don't guess.** Scripts produce every quantitative claim
  (format detection, request counts, chunk statistics, codec trials).
  Reserve judgment for interpretation and recommendations. Every deduction
  must be traceable to evidence in `findings.json`.
- **Expect access failures and make them informative.** In-region-only S3,
  EDL redirects, and requester-pays each have a distinct signature —
  diagnose using `references/access-and-auth.md` before declaring data
  inaccessible. An auth failure is `SKIPPED` (confidence downgrade), never
  a range-support `FAIL`.
- **One short intent exchange, then defaults.** Ask the two questions in
  Workflow step 2 in a single message with defaults offered; never
  interrogate progressively. If the user is absent or defers, apply the
  documented defaults and record the choice in the report — the assessment
  must be runnable unattended.
- **Match depth to the ask.** Full workflow for a provider preparing a
  formal assessment; for a quick "is this OK?", run probe + assess on the
  primary variables and offer the rest.

## Workflow

1. **Probe access first**: `python scripts/probe_access.py <INPUT>` —
   reachability, auth detection (EDL redirect is a *positive* detection
   signal), ranged-GET 206 verification (trust the 206 test over the
   `Accept-Ranges` header), object size, S3 bucket region, anonymous vs.
   authenticated. Range support is load-bearing: without it, Dimension D
   fails no matter how good the format is.
2. **Detect input type** (table below) and **elicit intent** in one
   exchange, skipping what the conversation already answers:
   - *Which variables/bands/columns matter?* ("all" / named / "you
     suggest"). Default: propose the primary geophysical variables from
     the dataset's own metadata and documentation — never QA flags or
     bounds — and say which you'll sample. Pass via `--variables`.
   - *What access pattern to optimize?* Map to
     `--profile interactive|training|agentic`. Default: assess all three
     (the report always shows all three regardless).
3. **Read the rubric** (`references/rubric.md`) before scoring anything,
   then the per-format reference(s) for formats actually encountered
   (see the reference list below — don't read all of them), plus
   `references/chunking-for-ai.md` (always, for Dimension C).
4. **Run the assessment**:
   ```bash
   python scripts/assess.py <INPUT> --workdir ./assessment \
       [--variables v1,v2] [--profile interactive|training|agentic] \
       [--max-assets N] [--all] [--emit-croissant] [--no-network]
   ```
   Detects input, crawls/samples, dispatches format assessors, runs the
   smoke test per sampled asset through the shared authenticated session,
   scores against the rubric, writes `findings.json`.
5. **Score governance from the catalog, not just the bare asset.** Before
   scoring Dimension E or B4 as missing, look for the dataset's catalog
   entry (STAC, NASA CMR, landing page — web search if available):
   license, DOI, contact, tutorials, and sample code usually live there.
   E6/E7 (usage and applications discoverability) are an *active search*,
   not a skip: report whether access code is discoverable or had to be
   guessed, with links.
6. **Render the report**:
   ```bash
   python scripts/report.py ./assessment/findings.json \
       --template assets/report-template.md -o ./assessment/assessment-report.md
   ```
7. **Review and hand over**: read the generated report; verify the verdict
   line is first, every failed/partial check has a remediation, and the
   three-profile chunking table is present (mandatory even for A-tier).
   Present both files with a one-paragraph verdict in chat — the user
   should never have to open the file to learn the answer.

## Input detection

| Input looks like | Treat as | Handling |
|---|---|---|
| URL ending `/stac/v1`, has `/search`, or conforms to STAC API | STAC API | `pystac-client` search; sample N per collection |
| `catalog.json` / `collection.json` / `item.json` | Static STAC | walk `child`/`item` links with `pystac` |
| `https://`, `s3://`, `gs://`, `az://` file or store root | Direct asset | detect format, assess directly |
| Local file or directory | Uploaded data | detect format(s); note hosting can't be evaluated from a local copy and ask whether a cloud-hosted location exists — prefer assessing that |
| JSON-LD with `@context` mentioning `mlcommons.org/croissant` | Croissant / GeoCroissant | resolve `distribution` to URLs; `references/croissant.md` |

Format detection order: extension → STAC media type → magic bytes via one
ranged read. Never download whole files. Catalog sampling: N=3 per
collection (first/middle/last by datetime), grouped by media type; record
the sample frame in findings. Mixed catalogs: grade per media type, never
a blended average.

Short-circuits: **HDF4** has no cloud-friendly random access — say so
plainly (in-region whole-file access or conversion are the options) but
still produce the report; hosting findings and the migration
recommendation are the value. Cloud-hostile formats (`references/legacy.md`)
always get a mandatory migration recommendation.

## Verdict and score

The report leads with a categorical verdict, then the numeric grade:

> **Verdict: <READY / READY WITH CAVEATS / NOT READY> — <one sentence
> naming the blocking finding(s) if any>.**
> Tier <A–F> · <score>/100 · confidence <High/Reduced/Low>

- **NOT READY** — any FAILed check a remote consumer would hit (smoke-test
  FAIL, chunk-size FAIL, no range support, data-integrity flags).
- **READY WITH CAVEATS** — partials/warnings only.
- **READY** — every check that could be assessed passes.

Rubric summary (full tables and check IDs in `references/rubric.md`):
A Format & structure 30 · B Metadata locality & richness 20 (B1
requests-to-open **measured, not inferred** — metadata dispersal across
the file is part of the evidence) · C Chunking & AI-workflow fit 25 ·
D Access & transport 15 (live-verified) · E Reproducibility & governance
10 (catalog-informed, per step 5). Tiers: 90–100 A · 75–89 B · 55–74 C ·
35–54 D · <35 F.

Hard rules: smoke-test FAIL caps at C-tier (SKIPPED ≠ FAILED — skips
downgrade confidence, never the score); being on S3 earns zero points by
itself — say "cloud-hosted ≠ cloud-optimized" in the verdict whenever the
gap applies; every failed check maps to ≥1 concrete remediation
(`h5repack -S PAGE`, `rechunker`, `gdal_translate -of COG`,
kerchunk/VirtualiZarr snippets); the three-profile chunking table closes
every report.

## Smoke test (mandatory, bounded)

`scripts/smoke_test.py` per sampled asset, hard-capped at ~25 MB transfer
and 60 s (caps enforced, breaches reported as their own finding):

1. HEAD + **authenticated ranged GET**: 206 + byte-count verification
   through the shared session; auth failures at either stage → `SKIPPED`
   with reason, never a range FAIL.
2. Instrumented lazy open counting HTTP **requests-to-open** and
   **bytes-to-open** (instrumentation must cover `read`, `readinto`, and
   block-cache paths — a 0-byte open of a multi-GB file is a measurement
   bug, not a pass).
3. Subset read of one **interior, data-bearing chunk** — sample away from
   swath/nodata margins; a fill-value chunk invalidates both throughput
   and compression telemetry. Variable steered by `--variables`.
4. **Empirical codec trials** on the decoded chunk, always including the
   dataset's **current codec as the baseline** row: zstd-1/3/5, blosc-lz4,
   blosc-zstd-3, ± byte shuffle — ratio and encode/decode throughput
   (interpretation: `references/compression.md`). Lossy precision control
   is surfaced as a provider decision, never a silent recommendation.
5. Format validation: `rio cogeo validate` (COG); consolidated metadata +
   one-chunk decode (Zarr); chunk/page introspection **and attribute read**
   for CRS/units/fill (HDF5 — grid mappings often live in a `projection`
   dataset, read it before scoring B2/B3); footer + row-group stats
   (Parquet).

Required libraries (degrade feature-by-feature, never crash): `rasterio`,
`rio-cogeo`, `zarr`, `xarray`, `h5py`, `fsspec`, `s3fs`, `pyarrow`,
`pystac`, `pystac-client`, `httpx`, `requests`.

## Outputs

- `assessment-report.md` — verdict line first, scorecard, per-asset
  findings with evidence, smoke telemetry, metadata-dispersal measurement,
  three-profile chunking table (current vs. recommended with expected
  request counts), prioritized remediations, discoverability links, rubric
  provenance.
- `findings.json` — sample frame, per-check evidence and scores, smoke
  telemetry. Schema stable for dashboards.
- Optional `croissant.jsonld` via `--emit-croissant`
  (`references/croissant.md`).

## References

Read when the step needs them, not before:

- `references/rubric.md` — scoring engine, check IDs, tiers, caps, provenance.
- `references/access-and-auth.md` — EDL/.netrc/TEA flows, shared-session design, S3 region and auth error diagnosis.
- `references/chunking-for-ai.md` — three-profile targets and sizing math; **compressed bytes on the wire**, not uncompressed, is the unit that matters.
- `references/compression.md` — codec grid, baseline protocol, decision rules, lossy options.
- Per-format: `cog.md`, `zarr.md`, `hdf5-netcdf.md`, `geoparquet.md`, `vector-pointcloud-tiles.md`, `legacy.md`, `croissant.md`.
- `references/acceptance-tests.md` — known-dataset regression assertions; run after any script change.

## Scripts

- `scripts/probe_access.py` — netrc-aware reachability, auth detection, range verification, S3 region.
- `scripts/assess.py` — crawl/sample → smoke test → score → `findings.json`.
- `scripts/smoke_test.py` — bounded live probes per asset (invoked by assess.py).
- `scripts/report.py` — render `findings.json` through `assets/report-template.md`.
