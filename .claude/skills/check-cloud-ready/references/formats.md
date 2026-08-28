# Format detection and per-format assessment

This is what `formats.py` does — not a script to run. The CLI sniffs format
automatically (`ext_hint` → `sniff_store`/`sniff_bytes` → `refine_hdf5`, in that
order) and asks you to confirm/override with `--format` only when detection is
ambiguous.

## Detection

File-based formats, by magic bytes (one small ranged read, `sniff_bytes`):

| First bytes | Format |
|---|---|
| `\x89HDF\r\n\x1a\n` | HDF5 — and therefore possibly NetCDF-4 |
| `CDF\x01` | NetCDF-3 classic |
| `CDF\x02` | NetCDF-3 64-bit offset |
| `CDF\x05` | NetCDF-3 64-bit data (CDF-5) |
| `\x0e\x03\x13\x01` | HDF4 |
| `II*\x00`/`MM\x00*` (+BigTIFF `II+\x00`/`MM\x00+`) | TIFF/COG (COG-ness not verified from bytes alone) |
| `GRIB` | GRIB2 |
| `PAR1` | Parquet (or kerchunk parquet refs) |
| `PK\x03\x04` | zip |
| `\x1f\x8b` | gzip |
| `ustar` at offset 257 | tar |
| JSON with a top-level `refs` key (or `version`+`templates`) | kerchunk/VirtualiZarr reference set |

- HDF5 files may start with a user block; the signature can sit at offset 512, 1024,
  2048, 4096 — `sniff_bytes` checks each via the caller's `offset_reads` callback.
- HDF5 vs NetCDF-4 (`refine_hdf5`): the `_NCProperties` root attribute, or a
  `DIMENSION_SCALE`-classed object, marks a NetCDF-4 file. Soft-imports h5py; without
  it, reports the unrefined `hdf5` guess plus a "refinement skipped" note.

Store-based formats, by layout probing (`sniff_store`):

- **Zarr v3**: `zarr.json` at the root; consolidated-metadata presence read from the
  same document.
- **Zarr v2**: `.zmetadata` (consolidated) or `.zgroup`/`.zarray` (unconsolidated —
  flagged as a metadata-dispersal finding: readers must crawl the store).
- **Icechunk**: a `refs/` prefix alongside `snapshots/` or `manifests/`.

The CLI's own format-class tables (`formats.CLOUD_NATIVE` /
`CLOUD_OPTIMIZABLE` / `CLOUD_HOSTILE`, mirrored in `scoring.py` for the A-class
rubric check):

- **Cloud-native**: cog, zarr, icechunk, parquet, copc, flatgeobuf, pmtiles.
- **Cloud-optimizable**: hdf5, netcdf4, las, kerchunk.
- **Cloud-hostile**: netcdf3, grib2, shapefile, csv, zip, tar, gzip, hdf4.

## Per-format notes

### HDF4
No practical random access for cloud tools — the format predates the ecosystem and
internal layout defeats range-based subsetting. The CLI still runs the hosting/access
checks and the CF/chunking checks it can; tell the provider directly: subsetting over
the network isn't practical. Options, in order: (1) compute in-region next to the
data, (2) convert to NetCDF-4/Zarr with cloud-appropriate chunking
(`references/legacy.md`), (3) a VirtualiZarr manifest sometimes works for large
static archives with regular internal layout.

### NetCDF-3
Header + contiguous variables; range requests work in principle, but there is no
chunking and no compression — every subset is a strided read over uncompressed data.
Chunk-size assessment doesn't apply; the CLI reports this rather than fabricating a
grade. See `references/legacy.md` for migration commands.

### NetCDF-4 / HDF5
The openers currently supported by this CLI (see `references/hdf5-netcdf.md`): chunk
size/orientation (`references/chunking.md`), metadata locality, and compression
(`references/compression.md`; gzip is common and usually beatable, but note netCDF
ecosystem compatibility constraints before recommending non-gzip codecs for `.nc`
files — the compatible route to better codecs is often "publish a Zarr/VirtualiZarr
copy").

### Zarr (v2/v3) and Icechunk
Native cloud format — assessment is pure measurement: consolidated metadata present?
coordinate chunking sane? chunk size/shape/orientation? codec? GeoZarr conformance
(`references/conventions.md`)? Icechunk is Zarr v3 + transactional storage; same
data-level assessment, plus note the version-control benefit. Access requires the
icechunk library — plain HTTP readers can't follow the manifest indirection; that's
an ecosystem-maturity caveat, not a failure.

### VirtualiZarr / kerchunk
Assess both layers: the reference store itself (metadata is consolidated by
construction) and the underlying referenced files (their internal chunking is
frozen — the manifest exposes it but can't fix too-small or too-large native
chunks). Chunk grading applies to the *native* chunks of the referenced files.

### COG / GeoTIFF
Opens via `rasterio` (GDAL's own VSI/curl machinery — not fsspec, so
requests/bytes-to-open are GDAL-estimated, not counted). `rio-cogeo` validation,
CRS, overviews, and per-band tile shape/codec are all real, measured evidence (see
`references/cog.md`). **Known caveat**: `chunking.py`'s compressed-size sampler only
knows how to read Zarr/HDF5 on-disk chunk storage, so a COG's chunk-size grade
currently falls back to an *estimated* size from the tile shape (assumed 2:1 ratio),
not each tile's real on-disk byte count (`TileByteCounts`). The opener also doesn't
populate per-band attrs, so `references/conventions.md`'s CF check has no signal to
work with for COG assets today (reports skipped/no-signal, not a false fail).

### GeoParquet / Parquet
Opens via `pyarrow`; footer parse, row-group stats, and each column's real
`total_compressed_size` (summed across row groups) are measured evidence (see
`references/geoparquet.md`). **Known caveat**: the opener doesn't expose row groups
as a chunk grid (`chunks` is `None` per column), so `chunking.py` currently grades a
Parquet column as one contiguous block using its full compressed size rather than a
per-row-group size — `references/geoparquet.md`'s row-group sizing guidance is
manual guidance today, not something this CLI measures per row group yet.

## Not yet supported by this CLI

**Cloud-native but not yet a supported opener**: COPC, FlatGeobuf, PMTiles. These
are cloud-native by format class (`formats.CLOUD_NATIVE`) — the layout itself is
fine — this CLI simply has no opener for them yet. Say so, run only the
hosting/access checks, and point at guide.cloudnativegeo.org for format-specific
guidance in the meantime.

**Cloud-hostile** (netcdf3, grib2, shapefile, csv, zip, tar, gzip, hdf4): always
assessable, always low — this is a format-class judgment, not a missing-opener gap
(none of these would score well even with a full opener). See `references/legacy.md`
for the mandatory migration commands per format.

STAC catalogs, STAC API search, and Croissant/GeoCroissant descriptor resolution are
explicitly **out of scope** for this CLI: it assesses one asset entrypoint (a path,
URL, or CMR granule ID) at a time, never a catalog crawl.
