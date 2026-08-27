# HDF5 / NetCDF-4 Assessor (incl. cloud-optimized HDF5)

Applies to: `.h5`, `.hdf5`, `.nc`, `.nc4`, media types
`application/x-hdf5`, `application/netcdf`. Magic bytes: `\x89HDF\r\n\x1a\n`
(possibly at a superblock offset of 512/1024/…). NetCDF-4 is HDF5 inside;
NetCDF-3 (`CDF\x01`/`CDF\x02`) is **not** — route it to `legacy.md`.

## Class baseline (Dimension A)

HDF5/NetCDF-4 is "cloud-optimizable": 
- **Optimized** (18–25): chunked datasets with ≥~1 MB chunks, paged
  aggregation with page size aligned to read block (order 2–16 MB; NASA
  ESDIS/IMPACT found ~8 MB works well), metadata compact/front-loaded, or
  fronted by a kerchunk/virtual-Zarr index (then also see `zarr.md`).
- **Not optimized** (8–17): default-written files — tiny chunks, metadata
  scattered through the file requiring dozens of small reads just to open.

## Format checks

| ID | Check | Dim | Criteria & evidence |
|---|---|---|---|
| H5-1 | Chunked storage | A | Datasets chunked, not one contiguous monolith (contiguous forces full-range reads for any subset) and not unchunked-compressed. Evidence: h5py `dataset.chunks`. |
| H5-2 | Chunk size ≥ ~1 MB | A, C | Uncompressed chunk size = ∏chunk_dims × itemsize. Tiny 10–100 KB chunks are the classic failure ⇒ fail with the computed size as evidence. |
| H5-3 | Paged aggregation | A, B1 | File-space strategy = PAGE with page size 2–16 MB (~8 MB per NASA findings), aligned to read block. Evidence: h5py `file.id.get_create_plist().get_file_space_strategy()` / page size; or infer from open behavior. |
| H5-4 | Metadata locality | B1 | Open (`xarray.open_dataset(..., engine="h5netcdf")` over fsspec) completes in a bounded number of requests. Dozens of small scattered-metadata reads ⇒ B1 fail; evidence: request count from instrumented open. |
| H5-5 | Virtual index available | A, B1 | If a kerchunk/VirtualiZarr reference set is published alongside, assess through it and credit "optimized" class. |
| H5-6 | CF conventions | B3 | units, standard_name, _FillValue, scale_factor/add_offset declared. |
| H5-7 | CRS | B2 | grid_mapping variable or catalog `proj:`. |
| H5-8 | Codec | C5 | gzip ok; szip/custom filters flagged (client availability); shuffle+zstd (via plugin) favored. |

## Smoke-test specifics

- Open with h5py over an instrumented fsspec file
  (`fsspec.open(url, "rb", block_size=…)`); count requests and bytes to
  open. Then `h5py` introspection replaces CLI `h5stat` (which may be
  absent): walk datasets, record `chunks`, dtype, shape, filters,
  and file-space strategy/page size where the API exposes it.
- Tiny subset: read one chunk of one dataset; record TTFB/throughput.
- Auth-gated files (e.g. NASA Earthdata Cloud) have three outcomes — never
  a FAIL, and never a reroute to the HTTPS URL:
  1. **Credentialed open** — a CMR granule ID or `/s3credentials` endpoint
     was supplied, so `smoke_test.py` mints credentials via obstore and reads
     the object directly over S3 (`transport: "s3-authenticated"`). Assessed
     normally, full confidence.
  2. **`nasa-credentials-required`** — protected NASA `s3://` URL with no
     granule ID / credentials endpoint: SKIPPED, confidence downgraded, and
     the report says which flag to re-run with.
  3. **`in-region-only`** — credentials minted, but S3 denied the read from
     outside `us-west-2`: SKIPPED, confidence downgraded; hosting itself is
     not penalized. Re-run from in-region compute for live evidence.

## Common remediations

- Scattered metadata / no paging (repack, ~8 MB pages):
  `h5repack -S PAGE -G 8388608 in.h5 out.h5`
- Tiny chunks (repack with sane chunking, per-dataset):
  `h5repack -l /path/to/var:CHUNK=1x512x512 in.h5 out.h5`
  (choose chunk dims from `chunking-for-ai.md` for the target profile)
- NetCDF route: `nccopy -c time/1,lat/512,lon/512 in.nc out.nc`
- Don't rewrite? Publish a virtual Zarr index:
  `kerchunk.hdf.SingleHdf5ToZarr(url).translate()` → combine with
  `kerchunk.combine.MultiZarrToZarr` or VirtualiZarr; store refs as
  parquet for large fleets.
- Reading cloud-optimized HDF5 efficiently: set fsspec `block_size` ≈ page
  size and enable caching (`cache_type="blockcache"`); with h5py ≥3.10 use
  `page_buf_size` matching the page size.
