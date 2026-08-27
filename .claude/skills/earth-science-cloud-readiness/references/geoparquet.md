# GeoParquet / Parquet Assessor (vector + tabular)

Applies to: `.parquet`, `application/x-parquet`,
`application/vnd.apache.parquet`. Magic bytes: `PAR1` at start AND end of
file (footer check needs a tail ranged read).

## Class baseline (Dimension A)

Parquet/GeoParquet is cloud-native (26–30): self-describing footer,
columnar, row-group-addressable via ranged reads.

## Format checks

| ID | Check | Dim | Criteria & evidence |
|---|---|---|---|
| PQ-1 | Valid footer | A, B1 | Footer parse succeeds from a tail ranged read (last ~64 KB usually suffices; footer length in last 8 bytes). Open = 1–2 requests ⇒ B1 pass. |
| PQ-2 | GeoParquet metadata | B2, B3 | `geo` key in file metadata: version, primary geometry column, CRS (PROJJSON), encoding (WKB / native geoarrow), bbox covering. Plain Parquet holding geometry as opaque WKB without `geo` metadata ⇒ B2 partial. |
| PQ-3 | Row-group sizing | C | Row groups sized for ranged reads: target ~64–256 MB uncompressed / roughly 8–64 MB compressed per row group for training-style scans; ≥ thousands of tiny row groups ⇒ fail (footer bloat + request storm). Evidence: `pyarrow.parquet.ParquetFile.metadata` row-group sizes. |
| PQ-4 | Column statistics & sorting | B1, C3 | Min/max stats present; spatially sorted (Hilbert/quadkey) or bbox column so predicate pushdown prunes row groups. Unsorted global vector dumps ⇒ every spatial query scans everything. |
| PQ-5 | Compression codec | C5 | ZSTD/Snappy favored; uncompressed flagged; heavy Brotli/GZIP flagged for read-heavy loads. |
| PQ-6 | Partitioning sanity | C4 | For datasets-of-files: sane partition scheme (not millions of KB-scale files, not one 500 GB monolith). |
| PQ-7 | Single schema | A | Consistent schema across files in the dataset; evolution documented. |

## Chunking mapping (Dimension C)

The row group is the chunk. Profiles:
- Interactive: small point lookups need stats/bbox pruning (PQ-4) more
  than tiny row groups; 8–32 MB compressed row groups fine.
- Training: 32–128 MB compressed row groups stream well; align sort order
  with sampling (e.g., spatial sort for tile sampling).
- Agentic: footer readable in 1 request + stats-based pruning ⇒ schema
  discovery in 1 request, subset in 1–2 more. Verify ≤3-request path.

## Smoke-test specifics

- `pyarrow.parquet.ParquetFile(fsspec_file)` with counting filesystem:
  requests/bytes-to-open (footer), then read ONE row group
  (`read_row_group(0, columns=[first_col])`) for TTFB/throughput.
- Record: num_row_groups, per-row-group compressed sizes (min/median/max),
  codecs, `geo` metadata JSON (truncated) as evidence.

## Common remediations

- No `geo` metadata: rewrite with GeoPandas ≥0.13 —
  `gpd.read_parquet(...) / gpd.GeoDataFrame(...).to_parquet("out.parquet", schema_version="1.1.0")`
  or `gdal_vectortranslate`/`ogr2ogr -f Parquet out.parquet in.ext`.
- Tiny row groups: rewrite with
  `pyarrow.parquet.write_table(table, ..., row_group_size=<rows targeting ~64MB>)`.
- No spatial ordering: sort by Hilbert curve / GeoHash before writing
  (e.g., DuckDB `ORDER BY ST_Hilbert(geom, ...)`), add bbox covering
  column (GeoParquet 1.1).
- Millions of small files: compact with DuckDB/Arrow dataset rewrite;
  consider PMTiles for pure visualization serving.
