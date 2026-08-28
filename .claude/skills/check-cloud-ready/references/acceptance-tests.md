# Acceptance tests (live checks + invariants)

These are manual/live checks against real public datasets — not part of the
automated `pytest` suite (which runs fully offline against fixtures/fakes). Run them
after any change that touches access resolution, format detection, chunking,
compression, or scoring, and whenever a DAAC changes its credentials endpoints or a
public bucket's layout.

## GREEN-gate checks

Each row is a real, currently-public entrypoint the CLI should handle end-to-end
without crashing, with the listed assertions holding.

| # | Entrypoint | Expected behavior |
|---|---|---|
| 1 | MUR SST Zarr store (`s3://mur-sst/zarr-v1`, public/anonymous, us-west-2) | `--anon` or auto-detected public access; format detected as `zarr`; `D1-range`/`D2-auth` pass; consolidated-metadata evidence recorded; chunk-size grade uses a **measured** (not estimated) compressed size for the selected variable. |
| 2 | NEX-GDDP-CMIP6 NetCDF4 file (`s3://nex-gddp-cmip6/NEX-GDDP-CMIP6/...`, public/anonymous, us-west-2) | Format refines to `netcdf4` (via `_NCProperties`/`DIMENSION_SCALE`, not left as bare `hdf5`); chunk size and orientation reported; CF check finds real `units`/`standard_name` signal. |
| 3 | A COG under `s3://sentinel-cogs/sentinel-s2-l2a-cogs/...` (public/anonymous, us-west-2 — also the codebase's own example of a bucket that correctly does **not** match `looks_like_nasa_earthdata`) | Format detected as `cog`; `rio-cogeo` validation evidence present; CRS present; chunk-size grade is explicitly labeled `estimated`, not `measured` (see `references/formats.md`'s COG caveat) — this is expected, not a bug. |
| 4 | NISAR granule `G4289749526-ASF` (`--granule-id G4289749526-ASF`, or as bare `INPUT`) | Resolves via CMR to credentials endpoint `https://nisar.asf.earthdatacloud.nasa.gov/s3credentials` and an `s3://sds-n-cumulus-prod-nisar-products/...` data URL (see `tests/test_resolve_granule.py`'s live check, `RUN_LIVE=1`). With no EDL credentials configured: `D2-auth` finding classified `credentials-endpoint-auth`, status `skipped`, remediation naming `EARTHDATA_TOKEN`/`EARTHDATA_USERNAME`+`EARTHDATA_PASSWORD`/`~/.netrc` — never a raw traceback. With valid credentials but run from outside `us-west-2`: classified `in-region-only`, hosting still reported, not treated as a hard failure. `cmr_meta` populated (E3–E7 scored from real UMM-JSON, not `skipped`). |
| 5 | Any of the above with `--no-network` | Every access probe and remote resolution step is skipped (`D0-network` finding); the run still completes and writes a report; confidence is `Reduced`/`Low`, never a crash; verdict is never fabricated from unavailable evidence. |

Cross-cutting invariants to spot-check on every GREEN-gate run:
- The report's Chunking recommendations section always shows all three profiles
  (interactive/training/agentic), even for an A-tier asset.
- Every `fail`/`partial` check in `findings.json` carries a non-empty remediation.
- No probe or sample ever exceeds `--byte-cap`/`--time-cap` (default 25 MB / 60 s);
  a breach is recorded in `budget_breaches`, never silently absorbed or left to
  crash the run.
- A smoke-test **FAIL** (data could not be read/verified live) caps the score at 74
  and forces verdict `NOT READY`; a smoke-test **SKIP** (no network, no auth) never
  caps the score — only the confidence label drops.
- `--json` output and `findings.json` on disk are byte-identical for the same run.

## Reworked from the earlier STAC/Croissant-catalog acceptance suite

This CLI assesses **one asset** per run (a path, URL, or CMR granule ID) — it has no
STAC-API search, no static-catalog crawl, and no Croissant/GeoCroissant resolution.
The rows dropped from the earlier two-skill acceptance suite (a STAC-API collection
sample, a static `catalog.json` crawl, a Croissant/GeoCroissant JSON-LD input, and
the mixed-catalog per-media-type rollup) don't apply here and are not replaced —
they described capabilities this package intentionally does not have.
