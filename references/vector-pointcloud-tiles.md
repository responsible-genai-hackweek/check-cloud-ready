# FlatGeobuf, COPC/LAS/LAZ, PMTiles Assessor

All three are cloud-native by design (Dimension A baseline 26–30) — each
embeds a spatial index or directory readable with a few ranged reads.
The work here is verifying the index is real and the internals are sane.

## FlatGeobuf (.fgb)

Magic bytes: `fgb` signature `\x66\x67\x62\x03\x66\x67\x62\x01`
(`fgb\x03fgb\x00`-family; check first 8 bytes start `fgb`).

| ID | Check | Dim | Criteria |
|---|---|---|---|
| FGB-1 | Packed Hilbert R-tree present | A, B1 | Header flag `index_node_size > 0`. Index absent ⇒ spatial queries scan whole file ⇒ A partial, B1 fail. |
| FGB-2 | Header parse via ranged read | B1 | Schema + CRS from first few KB. |
| FGB-3 | CRS declared | B2 | Header `crs` field populated. |
| FGB-4 | Bbox-query smoke | C3, D | One small bbox query over HTTP touches only a few ranges (verify request count). |

Remediation: rewrite with index —
`ogr2ogr -f FlatGeobuf out.fgb in.ext` (index on by default; ensure not
`-lco SPATIAL_INDEX=NO`).

## COPC / LAS / LAZ (.copc.laz, .las, .laz)

Magic bytes: `LASF`. COPC = LAZ 1.4 with `copc` VLR (id 1) carrying an
EPT-style octree; check VLR `user_id="copc"` in the header block.

| ID | Check | Dim | Criteria |
|---|---|---|---|
| COPC-1 | COPC VLR present | A | Plain LAS/LAZ without COPC octree ⇒ "cloud-optimizable, not optimized" (8–17): whole-file reads for any spatial subset. |
| COPC-2 | Octree page reads | B1, C1 | Root hierarchy page readable in 1–2 ranged reads; node point counts sane. |
| COPC-3 | CRS VLR | B2 | WKT VLR present. |
| COPC-4 | Chunk (node) sizes | C | Octree nodes typically ~1–8 MB compressed — matches interactive/agentic profiles; record observed node sizes. |

Remediations:
- LAS/LAZ → COPC: `pdal translate in.laz out.copc.laz --writers.copc.forward=all`
  or `untwine -i in.laz -o out.copc.laz --single_file`.
- Uncompressed LAS: always convert (LAZ/COPC), size evidence in report.

## PMTiles (.pmtiles)

Magic bytes: `PMTiles` (ASCII) at offset 0, version byte follows (v3).

| ID | Check | Dim | Criteria |
|---|---|---|---|
| PMT-1 | v3 header + root directory | A, B1 | Header (127 bytes) + root dir in first ranged read; clustered ordering flag set. |
| PMT-2 | Tile fetch | C1, D | One tile fetched via 2–3 ranged reads (header→dir→tile). Latency recorded. |
| PMT-3 | Internal compression | C5 | Tile data gzip/brotli per header; metadata JSON parseable. |
| PMT-4 | Intended use | — | PMTiles is a **visualization** container: full C1 credit, but C2 (training) is n/a — renormalize, and note that analysis/training should use the source data, not tiles. |

Remediation: MBTiles → PMTiles: `pmtiles convert in.mbtiles out.pmtiles`.

## Smoke-test specifics (all three)

- Verify magic bytes with the first ranged read (16 KB covers all
  headers).
- Issue the format's canonical "open" sequence and count requests: FGB
  header+index root; COPC header+root hierarchy page; PMTiles
  header+root directory. Each should be ≤3 requests — this doubles as the
  Dimension C3 agentic check.
- One tiny spatial subset (bbox / octree node / tile) for TTFB and
  throughput.
