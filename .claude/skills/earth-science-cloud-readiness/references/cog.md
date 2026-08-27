# COG / GeoTIFF Assessor

Applies to: `image/tiff; application=geotiff; profile=cloud-optimized`,
`image/tiff`, `.tif`, `.tiff`. Magic bytes: `II*\0` (little-endian) or
`MM\0*` (big-endian) in the first 4 bytes; BigTIFF: `II+\0` / `MM\0+`.

## Class baseline (Dimension A)

A **valid COG** is cloud-native (baseline 26–30). A plain (non-cloud-
optimized) GeoTIFF is "cloud-optimizable, not optimized" (8–17): readable
over HTTP but with striped layout, no overviews, or trailing IFDs it
forces many small reads.

## Format checks

| ID | Check | Dim | Criteria & evidence |
|---|---|---|---|
| COG-1 | rio-cogeo validation | A | `rio cogeo validate <url>` passes (or programmatic `rio_cogeo.cogeo.cog_validate`). Record errors + warnings verbatim. |
| COG-2 | Internal tiling | A | Tiled (not stripped). Typical valid tile sizes 256×256 or 512×512. Evidence: `TileWidth`/`TileLength` tags. Stripped GeoTIFF ⇒ fail. |
| COG-3 | Overviews present | A, C1 | Reduced-resolution IFDs present, factor-of-2 cascade down to ~≤512px. Missing overviews ⇒ C1 fail for rasters (mandatory for interactive profile). |
| COG-4 | Header/IFD placement | A, B1 | All IFDs at the front of the file so open needs 1–2 ranged reads of the first ~16–64 KB. Trailing IFDs (classic "GeoTIFF with appended overviews") ⇒ fail; evidence: byte offsets of IFDs from the header read. |
| COG-5 | Compression | A, C5 | Internal compression present: DEFLATE/ZSTD/LZW full credit. Uncompressed ⇒ partial (wastes bandwidth). WebP/JPEG (lossy) ⇒ flag; acceptable ONLY for visualization products — ask/infer intent, do not fail silently. |
| COG-6 | Single data type | A | One sample format per file; mixed/exotic sample formats or per-band type games ⇒ partial. |
| COG-7 | Nodata & scale/offset | B3 | Nodata declared; scale/offset tags if applicable. |
| COG-8 | CRS present | B2 | GeoKeys/WKT in file and/or STAC `proj:` extension. |

## Chunking mapping (Dimension C)

- The COG "chunk" is the internal tile × compression. Estimate compressed
  tile size from `TileByteCounts` (read via rasterio without pulling data).
- Interactive: 256–512 px tiles with overviews ⇒ full C1.
- AI training: COGs rarely hit 32–64 MB per read unit; that's fine —
  score C2 on whether a dataloader can assemble ~10–100 MB batches with
  few requests (large tiles, contiguous tile layout, or many parallel
  COGs). Note in recommendations that heavy training pipelines often
  convert COG mosaics → Zarr; only recommend that when training is the
  stated use case.
- Agentic: header-at-front + tiles of a few MB usually ⇒ strong C3;
  verify "≤3 requests to first subset" via smoke test (open + one tile).

## Smoke-test specifics

- Lazy open with `rasterio.open(url)`; count requests via GDAL
  (`CPL_CURL_VERBOSE` parse) or a counting fsspec layer with
  `/vsicurl/`-free HTTPS. Expect 1–2 requests-to-open for a valid COG.
- Subset: read one tile (e.g., `Window` matching the block size) —
  record TTFB, throughput.
- `rio cogeo validate` output goes into evidence verbatim.

## Common remediations

- Not a COG / trailing IFDs / no overviews / not tiled:
  `gdal_translate input.tif output.tif -of COG -co COMPRESS=DEFLATE -co BLOCKSIZE=512 -co OVERVIEWS=IGNORE_EXISTING`
- Recompress lossy → lossless for analysis products:
  `gdal_translate in.tif out.tif -of COG -co COMPRESS=ZSTD`
- Batch: `rio cogeo create in.tif out.tif --blocksize 512`
- Mosaic of thousands of small COGs hurting training throughput: build a
  STAC + stackstac/odc-stac loading recipe, or convert to Zarr with
  `stackstac` → `rechunker` (see `chunking-for-ai.md`).
