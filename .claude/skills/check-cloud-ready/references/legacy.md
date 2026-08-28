# Legacy / red-flag formats (always assessable, always low, migration mandatory)

These formats are cloud-hostile (`formats.CLOUD_HOSTILE`: netcdf3, grib2, shapefile,
csv, zip, tar, gzip, hdf4). Never refuse to assess them — grade them, explain *why*
the layout fights object storage, and attach a concrete migration plan. Distinguish
clearly in the verdict: these can be *cloud-hosted* (on S3) while being 0%
cloud-optimized — "cloud-hosted" and "cloud-optimized" are not the same claim.

Detection magic bytes (first ranged read — see `references/formats.md`):
- NetCDF-3 classic/64-bit: `CDF\x01`/`CDF\x02` (CDF-5: `CDF\x05`)
- HDF4: `\x0e\x03\x13\x01`. Caution: the `.hdf` extension is ambiguous — many `.hdf`
  files (especially NetCDF-4 era) are actually HDF5 (`\x89HDF\r\n\x1a\n`); the magic
  bytes, not the extension, decide. Common in NASA MODIS/MISR/AIRS-era archives.
- GRIB2: `GRIB` at offset 0 (edition byte 2 at offset 7)
- Shapefile (`.shp`): big-endian int 9994 in the first 4 bytes; the format is a
  *sidecar family* (`.shp`/`.shx`/`.dbf`/`.prj`) — that alone fails the
  metadata-locality check.
- ZIP: `PK\x03\x04` · GZIP: `\x1f\x8b` · TAR: `ustar` at offset 257
- CSV: no magic; extension/media type + sniff first KB.

## Why each fails

| Format | Core problem |
|---|---|
| NetCDF-3 | No chunking, record-variable interleaving; any subset ⇒ large sequential reads; no internal compression |
| HDF4 | Pre-cloud design: no paged metadata, layout scattered through the file, limited modern tooling (h5py cannot read it); remote partial reads devolve into many tiny seeks |
| GRIB2 (no index) | Message-sequential; finding one field means scanning messages; codecs (complex packing) slow; needs a `.idx` sidecar at minimum |
| Shapefile | Multi-file sidecars, 2 GB limits, no HTTP-friendly index, DBF encoding chaos |
| CSV dumps | No schema, no index, full-scan for everything, type guessing |
| ZIP/tar bundles | Container hides members; ZIP's central directory at EOF is *technically* range-readable but tools don't; tar has no index at all; compression is whole-stream |

Dimension C for these: score against the three profiles honestly (usually near zero
for training and agentic; an agent cannot go catalog→variables→subset in ≤3 requests
for any of them).

## Mandatory migration recommendations

- **HDF4 → HDF5 (then cloud-optimize) or COG**:
  `h4toh5convert in.hdf out.h5` (HDF Group h4h5tools), then
  `h5repack -S PAGE -G 8388608 out.h5 out-cloud.h5`; or for HDF4-EOS gridded
  products go straight to COG per subdataset:
  `gdal_translate -of COG HDF4_EOS:EOS_GRID:"in.hdf":grid:field out.tif`. For
  multi-file collections, prefer conversion to a consolidated Zarr store via xarray
  (`pyhdf`/`rioxarray` reader → `to_zarr`).
- **NetCDF-3 → NetCDF-4/Zarr**:
  `nccopy -k nc4 -d 4 -c time/1,lat/512,lon/512 in.nc out.nc4` (then treat as HDF5,
  see `references/hdf5-netcdf.md`), or straight to Zarr:
  ```python
  import xarray as xr
  ds = xr.open_dataset("in.nc")
  ds.chunk({"time": 512, "lat": 256, "lon": 256}).to_zarr("out.zarr",
      zarr_format=3, consolidated=True)
  ```
  Chunk targets from `references/chunking.md`.
- **GRIB2**: immediate mitigation — publish kerchunk references
  (`kerchunk.grib2.scan_grib`) so the fleet reads as virtual Zarr; durable fix —
  convert to Zarr (`xarray.open_dataset(engine="cfgrib")` → rechunk → `to_zarr`). At
  bare minimum publish `.idx` sidecars.
- **Shapefile → GeoParquet or FlatGeobuf**:
  `ogr2ogr -f Parquet out.parquet in.shp` (analysis) or
  `ogr2ogr -f FlatGeobuf out.fgb in.shp` (streaming/web).
- **CSV → (Geo)Parquet**: `duckdb -c "COPY (SELECT * FROM read_csv_auto('in.csv')) TO 'out.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)"`.
- **ZIP/tar**: unpack and publish members natively on object storage; if the
  archive must persist (provenance), publish alongside an index and unpacked
  mirrors. Never leave data *only* inside archives.

## Live-probe specifics

Still run hosting/access probes: HEAD + ranged reads verify hosting quality
(Dimension D can legitimately score well — that's the "cloud-hosted, not
cloud-optimized" verdict). Skip lazy-open instrumentation where no library can open
the format remotely; record that as evidence rather than raising an error.
