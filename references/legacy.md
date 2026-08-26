# Legacy / Red-Flag Formats (always assessable, always low, migration mandatory)

These formats are **cloud-hostile** (Dimension A baseline 0–7). Never
refuse to assess them; grade them, explain *why* the layout fights object
storage, and attach a concrete migration plan. Distinguish clearly in the
verdict: these can be *cloud-hosted* (on S3) while being 0% cloud-optimized.

Detection magic bytes (first ranged read):
- NetCDF-3 classic/64-bit: `CDF\x01` / `CDF\x02` (CDF-5: `CDF\x05`)
- HDF4: `\x0e\x03\x13\x01`. Caution: `.hdf` extension is ambiguous — many
  "`.hdf`" files (esp. NetCDF-4 era) are actually HDF5 (`\x89HDF\r\n\x1a\n`);
  the magic byte, not the extension, decides. Common in NASA MODIS/MISR/
  AIRS-era archives.
- GRIB2: `GRIB` at offset 0 (edition byte 2 at offset 7)
- Shapefile (.shp): big-endian int 9994 in first 4 bytes; but the format
  is a *sidecar family* (.shp/.shx/.dbf/.prj) — that alone fails B1.
- ZIP: `PK\x03\x04` · GZIP: `\x1f\x8b` · TAR: `ustar` at offset 257
- CSV: no magic; extension/media type + sniff first KB.

## Why each fails (put this reasoning in the report)

| Format | Core problem | Typical A score |
|---|---|---|
| NetCDF-3 | No chunking, record-variable interleaving; any subset ⇒ large sequential reads; no internal compression | 3–5 |
| HDF4 | Pre-cloud design: no paged metadata, Vdata/SDS layout scattered through the file, limited modern tooling (h5py cannot read it; needs pyhdf/GDAL); remote partial reads devolve into many tiny seeks | 2–5 |
| GRIB2 (no index) | Message-sequential; finding one field means scanning messages; codecs (complex packing) slow; needs `.idx` sidecar at minimum | 2–5 (with documented `.idx`: up to 8) |
| Shapefile | Multi-file sidecars, 2 GB limits, no HTTP-friendly index, DBF encoding chaos | 2–4 |
| CSV dumps | No schema, no index, full-scan for everything, type guessing | 1–4 |
| ZIP/tar bundles | Container hides members; ZIP central directory at EOF is *technically* range-readable but tools don't; tar has no index at all; compression is whole-stream | 0–3 |

Dimension C for these: score against profiles honestly (usually near 0 for
training and agentic; note that an agent cannot go catalog→variables→subset
in ≤3 requests for any of them).

## Mandatory migration recommendations

- **HDF4 → HDF5 (then cloud-optimize) or COG**:
  `h4toh5convert in.hdf out.h5` (HDF Group h4h5tools), then
  `h5repack -S PAGE -G 8388608 out.h5 out-cloud.h5`; or for HDF4-EOS
  gridded products go straight to COG per subdataset:
  `gdal_translate -of COG HDF4_EOS:EOS_GRID:"in.hdf":grid:field out.tif`.
  For multi-file collections, prefer conversion to a consolidated Zarr
  store via xarray (`pyhdf`/`rioxarray` reader → `to_zarr`).

- **NetCDF-3 → NetCDF-4/Zarr**:
  `nccopy -k nc4 -d 4 -c time/1,lat/512,lon/512 in.nc out.nc4` (then treat
  as HDF5, see `hdf5-netcdf.md`), or better, straight to Zarr:
  ```python
  import xarray as xr
  ds = xr.open_dataset("in.nc")
  ds.chunk({"time": 512, "lat": 256, "lon": 256}).to_zarr("out.zarr",
      zarr_format=3, consolidated=True)
  ```
  Chunk targets from `chunking-for-ai.md`.
- **GRIB2**: immediate mitigation — publish kerchunk references
  (`kerchunk.grib2.scan_grib`) so the fleet reads as virtual Zarr;
  durable fix — convert to Zarr (weather community pattern:
  `xarray.open_dataset(engine="cfgrib")` → rechunk → `to_zarr`). At bare
  minimum publish `.idx` sidecars.
- **Shapefile → GeoParquet or FlatGeobuf**:
  `ogr2ogr -f Parquet out.parquet in.shp` (analysis) or
  `ogr2ogr -f FlatGeobuf out.fgb in.shp` (streaming/web).
- **CSV → (Geo)Parquet**: DuckDB one-liner —
  `duckdb -c "COPY (SELECT * FROM read_csv_auto('in.csv')) TO 'out.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)"`.
- **ZIP/tar**: unpack and publish members natively on object storage; if
  the archive must persist (provenance), publish alongside an index and
  unpacked mirrors. Never leave data *only* inside archives.

## Smoke-test specifics

Still run it: HEAD + ranged reads verify hosting quality (Dimension D can
legitimately score well — that's the "cloud-hosted, not cloud-optimized"
verdict). Skip lazy-open instrumentation where no library can open the
format remotely; record `open_supported: false` as evidence rather than an
error.
