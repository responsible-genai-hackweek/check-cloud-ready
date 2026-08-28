{verdict_block}
# Cloud Readiness Assessment: {dataset_name}

**Input:** `{input_raw}` ({input_type}) · **Assessed:** {generated} · **Confidence:** {confidence}

{body}

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

Machine-readable evidence: `findings.json` alongside this report.
