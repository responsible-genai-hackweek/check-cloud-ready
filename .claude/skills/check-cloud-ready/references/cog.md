# COG / GeoTIFF assessor

Applies to: `image/tiff; application=geotiff; profile=cloud-optimized`,
`.tif`/`.tiff`. Magic bytes: `II*\x00` (little-endian) or `MM\x00*` (big-endian) in
the first 4 bytes; BigTIFF: `II+\x00`/`MM\x00+` — see `references/formats.md`.

The CLI opens COGs via `rasterio`/`rio-cogeo` (`references/formats.md`). Validity,
CRS, overviews, and codec are real measured evidence; chunk-size grading currently
falls back to an estimate rather than each tile's real on-disk byte count, and CF
checks have no per-band attrs to work with yet — see `references/formats.md`'s COG
note for the exact gap.

## Class baseline (Dimension A)

A **valid COG** is cloud-native. A plain (non-cloud-optimized) GeoTIFF is
"cloud-optimizable, not optimized": readable over HTTP but with striped layout, no
overviews, or trailing IFDs that force many small reads.

## Format checks

| Check | Dim | Criteria & evidence |
|---|---|---|
| rio-cogeo validation | A | `rio cogeo validate <url>` passes (or `rio_cogeo.cogeo.cog_validate`). Record errors + warnings verbatim. |
| Internal tiling | A | Tiled (not stripped). Typical valid tile sizes 256×256 or 512×512. Stripped GeoTIFF ⇒ fail. |
| Overviews present | A, C1 | Reduced-resolution IFDs, factor-of-2 cascade down to ~≤512px. Missing ⇒ fails the interactive profile (overviews are mandatory there). |
| Header/IFD placement | A, B1 | All IFDs at the front of the file so open needs 1–2 ranged reads of the first ~16–64 KB. Trailing IFDs (a GeoTIFF with appended overviews) ⇒ fail; evidence: byte offsets of IFDs. |
| Compression | A, C5 | DEFLATE/ZSTD/LZW ⇒ full credit — see `references/compression.md`. Uncompressed ⇒ partial. WebP/JPEG (lossy) ⇒ flag; acceptable only for visualization products — ask/infer intent, don't fail silently. |
| Single data type | A | One sample format per file; mixed/exotic per-band type games ⇒ partial. |
| Nodata & scale/offset | B3 | Declared — see `references/conventions.md`. |
| CRS present | B2 | GeoKeys/WKT in file. |

## Chunking mapping (Dimension C)

- The COG "chunk" is the internal tile × compression. Estimate compressed tile size
  from `TileByteCounts` (read via rasterio without pulling data).
- Interactive: 256–512 px tiles with overviews ⇒ full interactive-profile credit.
- AI training: COGs rarely hit the 32–64 MB training sweet spot per tile; that's
  fine — score on whether a dataloader can assemble ~10–100 MB batches with few
  requests (large tiles, contiguous tile layout, or many parallel COGs). Only
  recommend converting a COG mosaic to Zarr when training is the stated use case.
- Agentic: header-at-front + tiles of a few MB usually gives strong agentic-profile
  fit; verify "≤3 requests to first subset" via the live probe (open + one tile).

## Live-probe specifics

- Lazy open with `rasterio.open(url)`; count HTTP requests via GDAL
  (`CPL_CURL_VERBOSE`) or an instrumented fsspec layer. Expect 1–2 requests-to-open
  for a valid COG.
- Subset: read one tile (a `Window` matching the block size) — record TTFB and
  throughput.
- `rio cogeo validate` output goes into evidence verbatim.

## Common remediations

- Not a COG / trailing IFDs / no overviews / not tiled:
  `gdal_translate input.tif output.tif -of COG -co COMPRESS=DEFLATE -co BLOCKSIZE=512 -co OVERVIEWS=IGNORE_EXISTING`
- Recompress lossy → lossless for analysis products:
  `gdal_translate in.tif out.tif -of COG -co COMPRESS=ZSTD`
- Batch: `rio cogeo create in.tif out.tif --blocksize 512`
- Mosaic of thousands of small COGs hurting training throughput: convert to Zarr
  with `stackstac` → `rechunker` (see `references/chunking.md`).
