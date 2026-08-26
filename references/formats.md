# Format detection and per-format assessment

## Detection (what `detect_format.py` does)

File-based formats, by magic bytes (one small range request):

| First bytes | Format |
|---|---|
| `\x89HDF\r\n\x1a\n` | HDF5 — and therefore possibly NetCDF-4 |
| `CDF\x01` | NetCDF-3 classic |
| `CDF\x02` | NetCDF-3 64-bit offset |
| `CDF\x05` | NetCDF-3 64-bit data (CDF-5) |
| `\x0e\x03\x13\x01` | HDF4 |

- HDF5 files may start with a user block; the signature can sit at offset 512,
  1024, 2048... — the script checks the first few possible offsets.
- HDF5 vs NetCDF-4: presence of the `_NCProperties` root attribute (or netCDF
  dimension-scale structure) marks a NetCDF-4 file. Practically they assess the
  same; report the more specific name when known.

Store-based formats, by layout probing:

- **Zarr v2**: `.zgroup`/`.zarray` objects; `.zmetadata` present ⇒ consolidated
  (record this — it feeds the metadata-dispersal check).
- **Zarr v3**: `zarr.json` at the root.
- **Icechunk**: repo layout with `refs/`, `snapshots/`, `manifests/` prefixes.
- **VirtualiZarr / Kerchunk references**: a JSON file with a top-level `"refs"`
  key (kerchunk v0/v1), a parquet reference store, or an Icechunk repo containing
  virtual chunk refs. The *references* are cloud-ready only if the **underlying
  files** support ranges — probe one referenced target too.

## Per-format assessment notes

### HDF4 — short-circuit
No practical random access for cloud tools: the format predates the ecosystem,
libraries expect local POSIX access, and internal layout defeats range-based
subsetting. Tell the user directly: **subsetting over the network is not
practical**. Realistic options, in order: (1) compute in-region next to the data,
(2) convert to NetCDF-4/Zarr with cloud-appropriate chunking, (3) a VirtualiZarr
manifest *sometimes* works when the internal layout is regular — worth a try only
for large static archives. Still complete the hosting/access rows of the report.

### NetCDF-3
Header + contiguous variables; range requests work in principle but there is no
chunking and no compression — every subset is a strided read over uncompressed
data. Fine for small files; for large archives recommend conversion (NetCDF-4 or
Zarr) or a VirtualiZarr manifest (NetCDF-3's regular layout virtualizes well).
Chunk-size assessment doesn't apply; say so.

### NetCDF-4 / HDF5
The big three checks: chunk size (see chunking.md), **metadata layout** (files
written with default old-library settings scatter metadata → hundreds of reads to
open; `h5repack`/paged-aggregation fixes it; kerchunk/VirtualiZarr sidesteps it),
and compression (gzip is common and usually beatable — but note netCDF ecosystem
compatibility constraints before recommending non-gzip codecs for .nc files; the
compatible route to better codecs is often "publish a Zarr/VirtualiZarr copy").

### Zarr (v2/v3)
Native cloud format — assessment is pure measurement: consolidated metadata
present? coordinate chunking sane? chunk size/shape? codec? shard usage (v3
sharding lets small chunks live in big objects — if sharded, grade the *shard* as
the transfer unit and the inner chunk as the decompression unit). GeoZarr check
available (conventions.md).

### Icechunk
Zarr v3 + transactional storage. Same data-level assessment as Zarr; additionally
note version-control benefits and check that manifests aren't pathologically
fragmented. Access requires the icechunk library — plain HTTP readers can't
follow the manifest indirection; note that as an ecosystem-maturity caveat, not a
failure.

### VirtualiZarr
Assess both layers: the reference store (metadata consolidated by construction)
and the underlying files (their internal chunking is frozen — the manifest can't
fix too-small or too-large native chunks, only expose them). Chunk grading applies
to the *native* chunks of the referenced files. `vzviz` visualizes manifest
layout and simulates queries — use it when installed.

## Future / unsupported formats

- **Cloud-optimizable but not yet supported by this skill**: Cloud-Optimized
  GeoTIFF, GeoParquet, COPC, GRIB2, FlatGeobuf, PMTiles. Say so, run only the
  hosting/access probes, and point at guide.cloudnativegeo.org for their
  format-specific guidance.
- **Everything else** (tarballs, CSV dumps, bespoke binary): cannot be
  cloud-optimized in place; recommend conversion to an appropriate cloud-native
  format and stop.
