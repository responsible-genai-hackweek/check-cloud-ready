# HDF5 / NetCDF-4 assessor (incl. cloud-optimized HDF5)

Applies to: `.h5`, `.hdf5`, `.nc`, `.nc4`. Magic bytes: `\x89HDF\r\n\x1a\n` (possibly
at a superblock offset of 512/1024/2048/4096 — see `references/formats.md`).
NetCDF-4 is HDF5 inside; NetCDF-3 (`CDF\x01`/`CDF\x02`) is **not** — route it to
`references/legacy.md`.

## Class baseline (Dimension A)

"Cloud-optimizable": optimized (chunked, ≥~1 MB chunks) scores meaningfully above
not-optimized (contiguous, or chunked-but-tiny). See `references/rubric.md`'s
A-class check for the exact point math this CLI applies; the paged-aggregation
sub-check the original rubric allowed (file-space strategy = PAGE, 2–16 MB page
size, ~8 MB per NASA ESDIS/IMPACT findings) is a known, documented gap — the
opener's HDF5 inventory records don't currently capture file-space/page-size
telemetry, so it isn't scored today (see `rubric.md`'s "Known gaps").

## What's checked

- **Chunked storage** — datasets chunked, not one contiguous monolith (contiguous
  forces full-range reads for any subset).
- **Chunk size** (`references/chunking.md`) — measured, sampled compressed size
  against the one scored band table, plus the three-profile overlay.
- **Metadata locality** — lazy open should complete in a small, bounded number of
  requests; dozens of small scattered-metadata reads is a real failure mode of
  default-written HDF5/NetCDF-4 files.
- **CF conventions** (`references/conventions.md`) — units, standard_name,
  `_FillValue`, scale_factor/add_offset, coordinate identification.
- **CRS** — a `grid_mapping` variable, **or** (common in practice, especially NASA
  L2/L3 products like NISAR) a `projection` dataset carrying CRS/WKT information with
  no formal CF `grid_mapping` link at all. `conventions.py`'s grid-mapping check
  credits this as CRS-present evidence via its CRS-container fallback — worth calling
  out explicitly in a report as "CRS present via a `projection` dataset, not a CF
  `grid_mapping` reference" rather than a clean CF pass.
- **Codec** (`references/compression.md`) — gzip is common and acceptable for
  ecosystem compatibility, but usually beatable; szip/custom filters are flagged for
  client-availability concerns.

## Access notes specific to this format family

Many NASA HDF5/NetCDF-4 products are served through TEA (Thin Egress App) HTTPS
fronting rather than direct S3. TEA answers a plain HEAD request anonymously (so
reachability/size checks succeed with no credentials), but gates the ranged GET
behind Earthdata Login — see `references/access.md`'s note on
`classify_access_failure` correctly attributing that as an auth requirement, not a
"no range support" finding, since the server never got a chance to say whether it
supports ranges before the auth check fired.

## Common remediations

- Scattered metadata / no paging: `h5repack -S PAGE -G 8388608 in.h5 out.h5`
  (~8 MB pages).
- Tiny chunks: `h5repack -l /path/to/var:CHUNK=1x512x512 in.h5 out.h5` (pick chunk
  dims from `references/chunking.md`'s target for the stated profile). NetCDF route:
  `nccopy -c time/1,lat/512,lon/512 in.nc out.nc`.
- Don't rewrite? Publish a virtual Zarr index:
  `kerchunk.hdf.SingleHdf5ToZarr(url).translate()`, combined via
  `kerchunk.combine.MultiZarrToZarr` or VirtualiZarr; store refs as parquet for
  large fleets.
- Reading cloud-optimized HDF5 efficiently: set fsspec `block_size` ≈ page size and
  enable `cache_type="blockcache"`; with h5py ≥3.10 use `page_buf_size` matching the
  page size.
