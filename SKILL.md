---
name: check-cloud-ready
description: >
  Evaluate whether an earth science dataset is "cloud ready" — on object storage,
  randomly accessible via HTTP range requests, well-chunked, well-compressed, and
  standards-conformant — and produce a scorecard report with concrete fixes for the
  data provider. Use this skill whenever the user runs check-cloud-ready, asks
  whether a dataset is cloud optimized / cloud native / analysis ready, asks to
  assess chunking or compression of a NetCDF, HDF, Zarr, Icechunk, or VirtualiZarr
  dataset, or provides a dataset URL (s3://, https://, or a Zarr store) and wants to
  know how well it will perform for cloud access — even if they don't use the words
  "cloud ready".
---

# check-cloud-ready

Assess the cloud readiness of a dataset entrypoint (file path, HTTPS URL, S3 URL, or
Zarr/Icechunk store) and produce a scorecard the data provider can act on. The
audience is **data providers who can change file structure and embedded metadata** —
so every finding should end in a recommendation they can implement, not just a grade.

"Cloud ready" means four things, and the whole assessment hangs off them:

1. **On the cloud** — the data lives on object storage (S3, GCS, Azure Blob), not
   only behind a bespoke download portal.
2. **Randomly accessible** — tools can fetch and decompress *subsets* via HTTP range
   requests instead of downloading whole files.
3. **Well-chunked** — chunks are a reasonable size for cloud transfer (~8–16 MB
   compressed; see `references/chunking.md`), and optionally a good *shape* for the
   user's target access patterns.
4. **Standards-conformant** — standard tools can interpret the data because metadata
   follows conventions (CF; GeoZarr for Zarr).

## Ground rules

- **Never ask the user to type credentials into the conversation.** Earthdata Login
  and S3 credentials come from `~/.netrc` or environment variables — see
  `references/access-and-auth.md`. If credentials are needed and absent, pause and
  tell the user exactly what to set up, then continue once they confirm.
- **Measure, don't guess.** Use the bundled scripts for anything quantitative
  (format detection, access probing, chunk statistics, compression benchmarks).
  Reserve your own judgment for interpretation and recommendations.
- **Expect access failures and make them informative.** An S3 GET from outside the
  bucket's region can fail or be slow; a 403 may mean auth, region, or
  requester-pays. Diagnose before declaring a dataset inaccessible — the failure
  mode is itself a finding for the report.
- **Interview progressively.** Ask questions when a step genuinely needs the answer,
  not all upfront. Use the AskUserQuestion tool if available, otherwise plain text.
- **Match depth to the ask.** The full workflow serves a provider preparing a
  formal assessment. For a quick "is this OK?" check, run the probe, format
  detection, and chunk report, grade from those, and offer the compression
  benchmark and optional checks rather than running them unasked — most of the
  wall-clock cost is in the benchmark and the optional checks.

## Environment setup

The scripts degrade gracefully, but the full assessment wants:

```bash
pip install requests boto3 s3fs fsspec xarray zarr h5py netCDF4 numcodecs zstandard earthaccess --quiet
# (add --break-system-packages if pip refuses on an externally-managed environment)
```

Install lazily — each script tells you which import it's missing. `cfchecker` or the
IOOS `compliance-checker` are only needed if the user opts into the CF check.

## Workflow

### Step 1 — Triage the entrypoint

The user supplies an entrypoint. Classify it:

- **Local file/directory**: no network questions needed. Note in the report that the
  *hosting* criterion can't be evaluated from a local copy — then ask whether a
  cloud-hosted location exists for this data; if yes, prefer assessing that URL
  (chunking measured locally is identical, but access and hosting are the point).
- **HTTPS URL**: ask whether there is an object-storage (s3://...) location for the
  same data. Many providers front object storage with HTTPS — both may be worth
  probing. Also ask: is the data public, or does it require Earthdata Login (EDL) or
  other auth? If auth is required, follow `references/access-and-auth.md` before
  probing.
- **S3 URL (or GCS/Azure)**: ask public vs. authenticated. Warn the user up front
  that S3 access from outside the bucket's region may fail or be slow, and that this
  is expected — you'll diagnose it.
- **Zarr/Icechunk store URL**: same as above; store-level probing differs (many
  small objects rather than one file) and Step 3 will detect the flavor.

### Step 2 — Probe access

Run `scripts/probe_access.py <url>`. It reports: reachability, auth requirements
detected (e.g. EDL redirect), HTTP range-request support (`Accept-Ranges`, 206
responses), object size, and for S3 the bucket region and whether anonymous access
works. Interpret failures using the error guide in `references/access-and-auth.md` —
especially the in-region-only S3 case, which deserves a clear explanation to the
user rather than a raw stack trace.

Range-request support is the load-bearing result: without it, criterion 2 fails no
matter how good the format is.

### Step 3 — Identify the format

If the user knows the format, take their word for it (but sanity-check the magic
bytes anyway — it's one cheap request). Otherwise run
`scripts/detect_format.py <url>`, which reads header bytes and store layout to
distinguish: HDF4, HDF5, NetCDF-3 (classic/64-bit), NetCDF-4 (HDF5-based), Zarr v2,
Zarr v3, Icechunk, and VirtualiZarr/Kerchunk reference files.

Per-format assessment notes live in `references/formats.md`. Two short-circuits:

- **HDF4**: no cloud-friendly random access. Tell the user plainly: subsetting over
  the network isn't practical; the realistic options are in-region access to whole
  files, or conversion (NetCDF-4/Zarr, or a VirtualiZarr manifest if the internal
  layout permits). Still produce the report — the hosting criterion and the
  conversion recommendation are the value.
- **Unsupported formats** (COG, GeoParquet, COPC, GRIB2, FlatGeobuf, PMTiles, ...):
  say these are cloud-optimizable but not yet supported by this skill, and stop
  after the hosting/access checks. Anything else (CSV tarballs, bespoke binary):
  note it cannot be meaningfully cloud-optimized in place and recommend conversion.

### Step 4 — Choose assessment scope

Ask the user which variables to assess:

1. **A specific variable** they care about;
2. **All variables** — fan out subagents if available (one per variable or per
   group), otherwise loop; or
3. **Let the skill advise** — use your knowledge of the dataset (and web search if
   available) to pick the variables users actually read: the primary geophysical
   variables, not QA flags or bounds. State your reasoning.

Also ask the *optional* question: are there target use cases / query patterns to
optimize for (e.g. "time series at a point", "regional maps per timestep")? If yes,
chunk **shape** gets assessed in Step 5, not just size. If the
`access-pattern-analysis` skill is installed, use it to formalize the patterns.

### Step 5 — Assess chunking

Run `scripts/chunk_report.py <url> [--variables ...]`. It reports, per variable:
dtype, shape, chunk shape, chunks-per-array, uncompressed chunk size, sampled
*compressed* chunk size, and the codec pipeline. Grade against
`references/chunking.md`:

- **Target ~8–16 MB compressed per chunk** (provider-agnostic; acceptable ~4–64 MB;
  never below ~1 MB). Too small → per-request overhead and rate limits dominate; too
  large → wasted transfer on partial reads, slow retries, memory pressure.
- **Coordinate arrays**: tiny-chunked or unchunked-but-scattered coordinates force
  many small reads before any data flows; they should be readable in one request.
- **Metadata dispersal**: can a reader learn the full layout in O(1) requests
  (consolidated Zarr metadata, contiguous HDF5 metadata blocks)? NetCDF-4/HDF5 files
  written without care scatter metadata so badly that opening the file takes
  hundreds of reads — check and report this.
- **Chunk shape vs. use cases** (only if the user gave patterns): compute the chunks
  touched and read amplification for each stated pattern; a shape that's perfect for
  maps can be pathological for time series. `vzviz` can visualize this for
  VirtualiZarr manifests if installed.

### Step 6 — Assess compression

Run `scripts/compression_bench.py <url> --variable <var>` on representative chunks
(vary time/level/region where possible — ratio varies more across data content than
across codecs). It benchmarks the conventional grid — {zstd-1, zstd-3, zstd-5,
blosc-lz4, blosc-zstd-3} × {shuffle, noshuffle} — against the dataset's current
codec, reporting ratio and compress/decompress throughput.

Interpret with `references/compression.md`: the zero-evaluation default is **zstd
level 3 + byte shuffle**; over-the-internet reads favor ratio (zstd), in-region
high-throughput reads can favor decode speed (lz4-class); and the biggest win on
float data is usually **lossy precision control** (bit rounding / quantization),
which is a scientific judgment call to surface to the provider, never a silent
recommendation.

### Step 7 — Optional checks

Offer these; run the ones the user wants:

- **CF conventions** (`references/conventions.md`): quick structural check with
  xarray/cf-xarray heuristics, or the IOOS `compliance-checker` for a formal pass.
- **GeoZarr** (Zarr only, `references/conventions.md`): CF-style attributes,
  `grid_mapping`/CRS presence, multiscales if applicable. Note the spec's maturity
  honestly.
- **Usage discoverability**: could an agent *find out how to use this dataset*?
  Search for the dataset's landing page, sample code, tutorials. Report whether
  access code is discoverable or had to be guessed — this predicts how well AI
  agents and new users will fare.
- **Applications discoverability**: can common use cases/applications for the
  dataset be discovered? This informs whether an agent hunting for "a dataset for X"
  would ever land here.

### Step 8 — Write the scorecard

Produce `cloud-readiness-<dataset>.md` (and show a summary in chat). Use exactly
this structure so reports are comparable across datasets:

```markdown
# Cloud readiness: <dataset name>

**Verdict: <READY / READY WITH CAVEATS / NOT READY> — <one sentence saying why,
naming the blocking finding(s) if any>.**

**Entrypoint:** <url> · **Format:** <format> · **Assessed:** <date> · <variables assessed>

| # | Criterion | Result | Evidence |
|---|-----------|--------|----------|
| 1 | On object storage        | PASS/WARN/FAIL | ... |
| 2 | Random access (ranges)   | PASS/WARN/FAIL | ... |
| 3a| Chunk size               | PASS/WARN/FAIL | e.g. "2.1 MB compressed median (target 8–16 MB)" |
| 3b| Chunk shape for use cases| PASS/WARN/FAIL/not assessed | ... |
| 4a| CF conventions           | PASS/WARN/FAIL/not assessed | ... |
| 4b| GeoZarr (Zarr only)      | PASS/WARN/FAIL/not assessed | ... |
| — | Compression              | PASS/WARN/FAIL | current vs. best benchmarked |
| — | Usage discoverability    | noted/not assessed | ... |

## Findings
(one short section per non-PASS row: what was measured, why it matters)

## Recommendations for the data provider
(ordered by impact; each concrete enough to act on — e.g. "rechunk time from 1 to
240 per chunk (→ ~12 MB compressed)", "consolidate Zarr metadata", "switch
gzip-4 → zstd-3 + shuffle: same ratio, 4× faster decode")
```

Grades: **PASS** meets the guidance, **WARN** works but leaves performance on the
table, **FAIL** blocks cloud-native use. When a criterion couldn't be evaluated
(auth, region, local-only), say "not assessed — <why>" rather than guessing.

The verdict line is the report's first sentence because it's what the provider
came for; everything below it is justification. NOT READY = at least one FAIL a
consumer would hit (or a data-integrity flag like missing chunks); READY WITH
CAVEATS = WARNs only; READY = the criteria that could be assessed all pass. Repeat
the verdict in your chat summary — the user shouldn't have to open the file to
learn the answer.

## References

Read these when the step needs them, not before:

- `references/chunking.md` — chunk-size targets and why; coordinate/metadata pitfalls.
- `references/compression.md` — codec shortlist, benchmark grid, decision rules, lossy options.
- `references/formats.md` — detection details and per-format assessment specifics; future formats.
- `references/conventions.md` — CF and GeoZarr checks.
- `references/access-and-auth.md` — EDL via .netrc/earthaccess, S3 region/auth error diagnosis.

## Scripts

All scripts print JSON to stdout and human-readable notes to stderr; run with
`python3 scripts/<name>.py --help` for options.

- `scripts/probe_access.py` — reachability, auth detection, range-request support, S3 region.
- `scripts/detect_format.py` — magic bytes + store-layout format identification.
- `scripts/chunk_report.py` — per-variable chunk geometry and size statistics.
- `scripts/compression_bench.py` — codec grid benchmark on sampled chunks.
