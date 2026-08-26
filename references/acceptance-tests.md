# Acceptance Tests (prompts + expected behavior)

Run these after any change to scripts or rubric. Each row = a user prompt
the skill must handle end-to-end (assess → findings.json → report).

| # | Prompt / input | Expected behavior |
|---|---|---|
| 1 | "Is this collection cloud-ready?" + a public STAC API collection of COGs (e.g., a Planetary-Computer- or Earth-Search-style endpoint) | B/A tier; N=3 items sampled per collection (first/middle/last by datetime, recorded in sample frame); rio-cogeo validation output present as evidence; smoke telemetry with requests-to-open ≈ 1–2 |
| 2 | A Zarr store URL with consolidated metadata | requests-to-open ≈ 1–2 recorded in telemetry; consolidated-metadata evidence in ZAR-2/B1; one-chunk decode in subset_read; C3 agentic path ≤3 requests |
| 3 | An old NetCDF-3 or GRIB2 URL | F/D tier; verdict explicitly says "cloud-hosted ≠ cloud-optimized" if hosting is fine; MANDATORY migration plan with kerchunk + rechunk/`to_zarr` commands; Dimension D may still score well |
| 4 | A static `catalog.json` (no API) | Correct crawl via child/item links only — zero STAC-API assumptions (no /search calls); relative asset hrefs resolved against the item document URL |
| 5 | A Croissant/GeoCroissant JSON-LD file | `distribution` FileObjects/FileSets resolved to concrete URLs and assessed; declared sha256 credited in E2 as "declared, unverified"; with `--emit-croissant`, a GeoCroissant record is written with `cloudReadiness:*` properties and RAI sampling-frame note |
| 6 | Any input in a network-blocked environment (or `--no-network`) | Static-only report; smoke test status `SKIPPED` (never `FAILED`) with reason; report says "smoke test skipped, confidence: reduced"; Dimension D marked skipped at half credit, not zeroed |

Cross-cutting invariants to spot-check on every run:
- Report ends with the three-profile chunking table, even for A-tier data.
- Every `fail`/`partial` check in findings.json carries a remediation.
- Mixed catalogs show a per-media-type distribution table, never only an
  average.
- No probe downloads a whole file: per-asset budget ≤ ~25 MB / 60 s
  recorded under `smoke_test.budget`.
- Smoke-test FAIL (not SKIP) caps the asset at 74/C-tier
  (`score_capped_by_smoke_fail: true`).
