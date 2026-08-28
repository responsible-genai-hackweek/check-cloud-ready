# Zarr assessor (v2, v3, virtual Zarr, Icechunk, GeoZarr)

Applies to: store roots (prefix containing `zarr.json` [v3], `.zgroup`/`.zarray`/
`.zmetadata` [v2]), kerchunk reference files (`.json`/`.parquet` reference sets),
VirtualiZarr outputs, Icechunk repos (`refs/`+`snapshots/`/`manifests/` layout).

Detection (`formats.sniff_store`): probe `<root>/zarr.json`, then
`<root>/.zmetadata`, then `<root>/.zgroup`, with small ranged/existence checks — see
`references/formats.md`. Kerchunk JSON: top-level keys `{"version", "refs"}`.

## Class baseline (Dimension A)

Zarr v2/v3 and Icechunk are cloud-native. A kerchunk/VirtualiZarr index **in front
of** archival HDF5/NetCDF puts the *dataset* in "cloud-optimizable AND optimized"
territory — the virtual layer fixes access, but the underlying chunk layout is
frozen; say so in the verdict.

## Format checks

| Check | Dim | Criteria & evidence |
|---|---|---|
| Version & spec conformance | A | v3 (`zarr.json`) or v2 (`.zgroup`/`.zarray`). Record version. |
| Consolidated metadata | A, B1, C3 | v2: `.zmetadata` present. v3: `consolidated_metadata` in root `zarr.json`. Absent ⇒ per-array walk ⇒ metadata-locality fail; evidence: requests-to-open count. |
| Sharding when chunk count is huge | A, C4 | v3 sharding codec (`sharding_indexed`) used when total chunk count is large (rule of thumb: >~1M objects). |
| Coordinate arrays present | B3 | Dimension coordinates stored as arrays (`_ARRAY_DIMENSIONS` [v2/xarray] or v3 `dimension_names`) so xarray-class tools open with labeled dims — see `references/conventions.md`. |
| Codec readability | A, C5 | No unreadable custom codecs; numcodecs/zarr-python-standard codecs. ZSTD/LZ4/Blosc favored for read-heavy loads — see `references/compression.md`. |
| CF/GeoZarr conventions | B2, B3 | CF attributes (units, standard_name, calendar), CRS via `grid_mapping` or a CRS-container variable — see `references/conventions.md`. |
| Chunk layout vs. profiles | C1–C3 | Compressed chunk size (measured, sampled) and orientation — see `references/chunking.md`. Classic failure to name explicitly: one-timestep-per-chunk global fields when the use case is per-pixel time series. |
| Icechunk versioning | E1 | Icechunk snapshots/tags present ⇒ full credit for reproducibility versioning. |
| Virtual Zarr reference health | A, D | Kerchunk refs resolve to live URLs; a broken/expired ref is a D4 (clean-path) fail. |

## Live-probe specifics

- Open via `xarray.open_zarr(store, consolidated=None)` (or the equivalent
  instrumented open this CLI performs): record requests-to-open, bytes-to-open,
  time-to-open. Consolidated stores should show ≈1–2 requests-to-open;
  unconsolidated stores show O(arrays) — record the number as evidence.
- Decode one interior, data-bearing chunk (`references/chunking.md`'s sampling
  approach) to verify codec readability and measure the real compression ratio.
- Agentic sub-check: can catalog → variable list → one subset read be done in ≤3
  requests? For consolidated Zarr: 1 (metadata) + 1 (chunk) = 2.

## Common remediations

- No consolidated metadata (v2): `zarr.consolidate_metadata(store)`.
- Bad chunk shape/size: a `rechunker` recipe —
  ```python
  from rechunker import rechunk
  plan = rechunk(source_array, target_chunks={"time": 512, "y": 256, "x": 256},
                 max_mem="2GB", target_store="s3://…/rechunked.zarr",
                 temp_store="s3://…/tmp.zarr")
  plan.execute()
  ```
  (pick target chunks from `references/chunking.md` for the stated profile.)
- Millions of objects: migrate to Zarr v3 with sharding (shard = ~64–256 MB
  containing many ~1–16 MB inner chunks).
- Archival HDF5/NetCDF fleet, rewrite not feasible: build virtual Zarr —
  ```python
  import virtualizarr as vz
  vds = vz.open_virtual_dataset("s3://bucket/file.nc")
  vds.virtualize.to_kerchunk("refs.json", format="json")   # or parquet refs
  ```
- No versioning: adopt Icechunk (transactional snapshots over object storage) for
  mutable stores.
