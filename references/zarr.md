# Zarr Assessor (v2, v3, virtual Zarr, Icechunk, GeoZarr)

Applies to: store roots (prefix containing `zarr.json` [v3], `.zgroup` /
`.zarray` / `.zmetadata` [v2]), `application/vnd+zarr`, kerchunk reference
files (`.json` / `.parquet` reference sets), VirtualiZarr outputs, Icechunk
repos (`icechunk.json` / snapshot layout).

Detection: probe `<root>/zarr.json`, then `<root>/.zmetadata`, then
`<root>/.zgroup` with small ranged GETs. Kerchunk JSON: top-level keys
`{"version", "refs"}`. Icechunk: repo config object at the root.

## Class baseline (Dimension A)

Zarr v2/v3 and Icechunk are cloud-native (26–30). A kerchunk/VirtualiZarr
index **in front of** archival HDF5/NetCDF puts the *dataset* in
"cloud-optimizable AND optimized" (18–25) — the virtual layer fixes access
but the underlying chunk layout is frozen; say so in the verdict.

## Format checks

| ID | Check | Dim | Criteria & evidence |
|---|---|---|---|
| ZAR-1 | Version & spec conformance | A | v3 (`zarr.json`, node metadata) or v2 (`.zgroup`/`.zarray`). Record version. |
| ZAR-2 | Consolidated metadata | A, B1, C3 | v2: `.zmetadata` present. v3: consolidated metadata in root `zarr.json` (or single-request enumerable hierarchy). Absent ⇒ per-array `.zattrs`/`.zarray` walk ⇒ B1 fail; evidence: requests-to-open count. |
| ZAR-3 | Sharding when chunk count is huge | A, C4 | v3 sharding codec (`sharding_indexed`) used when total chunk count is large (rule of thumb: >~1M objects). Millions of tiny objects without shards ⇒ C4 fail. Evidence: computed chunk count = ∏(ceil(shape/chunks)) across arrays. |
| ZAR-4 | Coordinate arrays present | B3 | Dimension coordinates stored as arrays (`_ARRAY_DIMENSIONS` [v2/xarray] or v3 dimension_names) so xarray opens with labeled dims. |
| ZAR-5 | Codec readability | A, C5 | No unreadable custom codecs; numcodecs/zarr-python-standard codecs. ZSTD/LZ4/Blosc favored for read-heavy AI loads; slow high-ratio codecs flagged. |
| ZAR-6 | CF/GeoZarr conventions | B2, B3 | CF attributes (units, standard_name, calendar), CRS via `grid_mapping` / GeoZarr conventions or STAC `proj:`. GeoZarr conformance noted when claimed. |
| ZAR-7 | Chunk layout vs profiles | C1–C3 | Compute compressed chunk size ≈ (∏chunks × dtype.itemsize) × observed ratio (from the one decoded chunk in smoke test; assume 2:1 if unknown, and say so). Score per `chunking-for-ai.md`. Classic failure to name explicitly: one-timestep-per-chunk global fields when the use case is per-pixel time series. |
| ZAR-8 | Icechunk versioning | E1 | Icechunk snapshots/tags present ⇒ E1 full credit. |
| ZAR-9 | Virtual Zarr reference health | A, D | Kerchunk refs resolve to live URLs; sample a few refs with ranged reads. Broken/expired refs ⇒ D4 fail. |

## Smoke-test specifics

- Open with a counting fsspec filesystem:
  `xarray.open_zarr(store, consolidated=None)` — record requests-to-open,
  bytes-to-open, time-to-open. Consolidated stores should show ≈1–2
  requests-to-open; unconsolidated stores show O(arrays) requests — record
  the number as evidence.
- Decode exactly one chunk of one array (smallest array first) to verify
  codec readability and measure the real compression ratio + TTFB.
- Agentic sub-check (C3): can catalog → variable list → one subset read be
  done in ≤3 requests? For consolidated Zarr: 1 (metadata) + 1 (chunk) = 2. 

## Common remediations

- No consolidated metadata (v2): `zarr.consolidate_metadata(store)`.
- Bad chunk shape/size: `rechunker` recipe —
  ```python
  from rechunker import rechunk
  plan = rechunk(source_array, target_chunks={"time": 512, "y": 256, "x": 256},
                 max_mem="2GB", target_store="s3://…/rechunked.zarr",
                 temp_store="s3://…/tmp.zarr")
  plan.execute()
  ```
  (Pick target chunks from `chunking-for-ai.md` for the stated profile.)
- Millions of objects: migrate to Zarr v3 with sharding
  (shard = ~64–256 MB containing many ~1–16 MB inner chunks).
- Archival HDF5/NetCDF fleet, rewrite not feasible: build virtual Zarr —
  ```python
  import virtualizarr as vz
  vds = vz.open_virtual_dataset("s3://bucket/file.nc")
  vds.virtualize.to_kerchunk("refs.json", format="json")   # or parquet refs
  ```
- No versioning: adopt Icechunk (transactional snapshots over object
  storage) for mutable stores.
