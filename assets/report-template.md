# Cloud Readiness Assessment: {dataset_name}

**Assessed:** {timestamp} · **Input:** `{input_raw}` ({input_type}) ·
**Confidence:** {confidence}

## Executive summary

**Tier: {tier} — {tier_label} · Score: {score}/100**

{verdict_paragraph}

## Scorecard

{scorecard_table}

{mixed_catalog_section}

## Per-asset findings

{per_asset_findings}

## Smoke-test telemetry

{smoke_telemetry_table}

{smoke_skip_note}

{compression_section}

## Chunking recommendations (all three profiles)

> Within-chunk partial reads are impossible by design — the chunk is the
> atomic read unit — so oversized chunks tax every partial read and
> undersized chunks tax every bulk read.

{chunking_table}

{chunking_notes}

## Prioritized remediation

{remediation_list}

## Sample frame (reproducibility)

{sample_frame}

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
