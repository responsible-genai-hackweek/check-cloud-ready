# Merge plan: check-cloud-ready + earth-science-cloud-readiness → assess-cloud-readiness

`SKILL.md` in this directory describes the **target state**. This document
maps every file to its source, lists the script fixes required (each traced
to a failure observed in the 2026-08-26 live test against the NISAR L2 GCOV
granule — reports in `test-output/`), and defines the acceptance test that
gates deployment.

**Status: not yet assembled.** The scripts and references below have not
been copied/fixed; invoking this skill before completing this plan will
fail.

## Design summary

Keep earth-science-cloud-readiness's **engine** (one-command
`assess.py → findings.json → report.py` pipeline, A–E rubric with numeric
score and tiers, bounded smoke test, SKIPPED-vs-FAILED with confidence
labels, three-profile AI chunking table, broad format coverage incl.
STAC/COG/GeoParquet/Croissant). Graft in check-cloud-ready's **judgment
layer** (categorical provider-facing verdict as the report's first line,
netrc-aware auth handling from `probe_access.py` extended to every script,
metadata-dispersal as measured evidence, current-codec-as-baseline
compression trials, catalog/web-search-informed governance and
discoverability, "trust the 206 test over the header" access diagnostics).
Fix the shared defects both skills exhibited (EDL auth, fill-value chunk
sampling).

## File provenance

| Target file | Source | Action |
|---|---|---|
| `SKILL.md` | new (this merge) | done — written 2026-08-26 |
| `scripts/probe_access.py` | check-cloud-ready | copy as-is (only script in either skill that handled EDL correctly); factor its netrc/redirect session handling into a shared `auth.py` helper for the other scripts |
| `scripts/assess.py` | earth-science | copy + fixes S3, S7 |
| `scripts/smoke_test.py` | earth-science | copy + fixes S1, S2, S4, S5, S6, S8 |
| `scripts/report.py` | earth-science | copy + fix R1 |
| `assets/report-template.md` | earth-science | copy + fixes R1–R3 |
| `references/rubric.md` | earth-science | copy + edits E1, E2 |
| `references/access-and-auth.md` | check-cloud-ready | copy + edit A1 (praised in testing: predicted the exact EDL/in-region failure modes seen) |
| `references/chunking-for-ai.md` | earth-science | copy + merge in check-cloud-ready `chunking.md`'s compressed-bytes-on-the-wire clarification and coordinate-array guidance |
| `references/compression.md` | merge of both | earth-science base + check-cloud-ready's baseline protocol and "lossy is a provider decision, never silent" rule |
| `references/cog.md`, `zarr.md`, `geoparquet.md`, `vector-pointcloud-tiles.md`, `legacy.md`, `croissant.md` | earth-science | copy as-is |
| `references/hdf5-netcdf.md` | earth-science | copy + note that CRS often lives in a `projection` dataset (not attrs) and that TEA fronting answers HEAD anonymously |
| `references/acceptance-tests.md` | earth-science | copy + add the NISAR GCOV case below |
| `references/formats.md`, `detect_format.py`, `chunk_report.py`, `compression_bench.py`, `conventions.md` (check-cloud-ready) | — | retire; capabilities absorbed by assess.py/smoke_test.py per fixes below. Port `conventions.md`'s CF structural-check heuristics into `hdf5-netcdf.md`/`zarr.md` before deleting |

## Script fixes

Each fix cites the observed failure. "CCR" = check-cloud-ready run,
"ESCR" = earth-science-cloud-readiness run.

- **S1 — Shared authenticated session (blocker).** Both skills' fsspec/
  aiohttp/httpx scripts returned 401 on EDL-protected URLs despite valid
  `~/.netrc`; each test agent had to invent a workaround (CCR: pre-resolved
  signed CloudFront URL; ESCR: `sitecustomize.py` cookie shim +
  `FSSPEC_HTTP`). Fix: a shared `auth.py` that pre-authenticates once with
  `requests`+netrc (which works — verified 206), captures the TEA session
  cookie / resolved URL, and injects it into fsspec and httpx sessions used
  by every script. Document in `access-and-auth.md` (edit A1).
- **S2 — Check auth at GET, not HEAD.** ESCR `smoke_test.py:189` looks for
  401/403 only at HEAD; ASF TEA answers HEAD anonymously, so the ranged
  GET's 401 was recorded as a *range-support failure* → hard smoke FAIL →
  C-tier cap with wrong evidence, instead of the documented
  `SKIPPED, reason=auth`. Fix: classify 401/403/redirect-to-URS at any
  stage as auth; only a non-auth non-206 is a range failure.
- **S3 — HDF5 codec detection.** ESCR `assess.py:693` reads
  `lazy_open["compression"]`, absent for HDF5 (per-dataset values are in
  `lazy_open["datasets"][*]["compression"]`); reported "no compression
  observed" + FAIL on a fully gzip-compressed file. Fix the key path; the
  correct branch is gzip → "recompress zstd+shuffle" remediation.
- **S4 — Interior, data-bearing chunk sampling.** Both skills sampled
  fill/NaN-dominated chunks (ESCR: corner chunk → meaningless 20971×
  ratio quoted in the report; CCR: median 4 KB "compressed size" and
  nonsense bitround error stats). Fix: sample chunks near the array
  centroid and reject candidates whose decoded content is ≥90% fill/NaN
  (retry up to N interior offsets; report if none qualify).
- **S5 — Requests/bytes-to-open instrumentation.** ESCR `_CountingFile`
  wraps only `read()`; h5py's fileobj driver uses `readinto()`, so a
  3.3 GB file scored a perfect B1 on "1 request / 0 bytes to open". Fix:
  wrap `read`, `readinto`, and the fsspec block-cache fetch path; assert
  bytes_to_open > 0 when open succeeded.
- **S6 — HDF5 attribute/CRS reading.** ESCR never reads HDF5 attrs or the
  `projection` dataset, scoring B2/B3 as fails on a file with full
  grid_mapping + WKT/EPSG (~7 pts of false negatives; CCR's manual check
  confirmed CF-1.7 compliance). Fix: read attrs on sampled datasets and
  recognized CRS containers before scoring B2/B3.
- **S7 — Metadata-dispersal measurement.** CCR calls this load-bearing for
  HDF5 but ships no script (its agent hand-measured 1,631 requests to walk
  the 343-object tree); ESCR doesn't measure it at all, and its 25-dataset
  visit cap stopped before `/science/LSAR/identification` where NISAR
  keeps granule metadata. Fix: add a bounded full-tree metadata walk
  (request + wall-clock capped) to assess.py, recorded as B1 evidence;
  visit identification/metadata groups before the cap bites.
- **S8 — Enforce the smoke-test caps.** ESCR's 60 s per-asset cap is
  checked only inside `spend()` and was exceeded (65.1 s). Fix: enforce
  wall-clock at stage boundaries; a breach is its own reported finding
  ("dataset forces oversized reads"), per SKILL.md.
- **S9 — Current codec as trial baseline.** CCR's `compression_bench.py`
  never benchmarked the current codec despite its SKILL.md saying it does
  (agent hand-measured gzip to grade honestly). The merged codec trial
  grid must include the dataset's current codec as row one.
- **S10 — Variable matching UX.** CCR: `chunk_report.py` silently returned
  `{"variables": {}}` for leading-slash paths while `compression_bench.py`
  threw a raw h5py KeyError for basenames. Merged matcher: accept full
  paths and basenames/substrings uniformly (ESCR's substring matching
  worked well); on zero matches, error with "no variables matched;
  available: ..." rather than empty output.

## Report / template changes

- **R1 — Verdict line first.** Prepend the categorical
  READY / READY WITH CAVEATS / NOT READY sentence (mapping defined in
  SKILL.md "Verdict and score") above the tier/score in both
  `report-template.md` and `report.py`'s executive summary.
- **R2 — Metadata-dispersal row** in the scorecard (B1 evidence: requests
  to open, requests for full metadata walk).
- **R3 — Discoverability section**: E6/E7 become active findings with
  links found (CCR's run surfaced 8 real NISAR resources; ESCR scored
  E6 as a 0.5 skip on the same dataset). Rubric edit E1: E6/E7 pass
  criteria say "search the catalog entry AND the web when available"
  instead of skipping bare assets. Rubric edit E2: note that Dimension E
  items (license, DOI, contact) must be checked against the dataset's
  catalog (CMR/STAC/landing page) before scoring 0 from the bare asset.

## Interaction design (from the unattended-run logs)

The CCR run hit 5 blocking would-ask-the-user points; ESCR hit 2. Keep
ESCR's single-exchange elicitation with documented defaults (variables =
primary geophysical variables proposed from metadata; profile = all three)
so the skill degrades gracefully to unattended/agentic use — this is
encoded in SKILL.md's ground rules.

## Acceptance test (GREEN gate)

Re-run the merged skill unattended against the 2026-08-26 test input:

```
https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001/NISAR_L2_PR_GCOV_028_143_D_070_4005_DHDH_A_20260823T030458_20260823T030521_P05023_N_P_J_001.h5
```

with `--variables HHHH,HVHV` and valid `~/.netrc`. Assert (baselines from
`test-output/`):

1. All scripts authenticate via netrc with no manual workaround; smoke
   test = pass (not FAIL-as-range, not SKIPPED). [S1, S2]
2. Current codec detected as gzip(+shuffle); trial grid includes a gzip
   baseline row; no "no compression observed". [S3, S9]
3. Sampled chunk is data-bearing: compression ratio < 100× (CCR measured
   ~1.65–2× on real data; 20971× = fill chunk). [S4]
4. requests_to_open ≥ 3 and bytes_to_open > 0 (CCR measured 3 requests to
   open). [S5]
5. B2 (CRS) passes — the file carries full grid_mapping + WKT/EPSG. [S6]
6. Metadata-dispersal evidence present; full-walk request count on the
   order of CCR's 1,631 measurement. [S7]
7. Per-asset wall clock ≤ 60 s or an explicit cap-breach finding. [S8]
8. Report first line: **NOT READY** (chunk-size FAIL: 512×512 → ≪1 MB
   compressed) with D-tier ±1 tier and "cloud-hosted ≠ cloud-optimized"
   language; three-profile table present; every fail has a remediation
   incl. rechunk to ~2048×2048 and paged aggregation / kerchunk-Zarr
   companion. [R1]
9. E6/E7 report actual links (User Guide, MAAP docs, CMR entry), not a
   skip. [R3]
10. Run a `--no-network` pass: everything network-dependent is SKIPPED
    with confidence downgraded, score not zeroed.

Then run the merged variable-matching cases: full path, basename, and a
bogus name (expect the "available: ..." error). [S10]

Add this case to `references/acceptance-tests.md` so future script edits
re-verify it.

## Open questions for the maintainer

- Final skill name: `assess-cloud-readiness` chosen to avoid colliding
  with the existing `check-cloud-ready`; rename freely (the repo name
  suggests `check-cloud-ready` may be the keeper once the parents retire).
- Retire both parent skill directories after the acceptance test passes,
  or keep them for A/B comparison?
- CCR's optional formal IOOS compliance-checker pass (needs full download)
  — keep as an opt-in documented in `hdf5-netcdf.md`, or drop?
