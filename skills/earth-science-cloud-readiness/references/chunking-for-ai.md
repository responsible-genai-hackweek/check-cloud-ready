# Chunking for AI Workflows — Targets & Sizing Math

Used for Dimension C scoring and for the **mandatory three-profile
recommendation table** that ends every report (even A-tier). Grounded in
Pangeo ARCO chunking guidance and Zarr v3 sharding behavior.

## The one physical law

**Within-chunk partial reads are impossible by design.** The chunk (tile,
shard-inner-chunk, row group, octree node) is the atomic unit of I/O:
reading one value decompresses the whole chunk. Therefore oversized chunks
tax every partial read, and undersized chunks tax every bulk read with
per-request latency and object-count overhead. All targets below are
*compressed* sizes unless stated.

## Profile targets

| Profile | Chunk size target | Dominant cost | Non-negotiables |
|---|---|---|---|
| **Interactive / visualization** | ~1–4 MB | latency (human waits per click) | overviews/pyramids for rasters; index for vectors |
| **AI training (batch throughput)** | ~10–100 MB, sweet spot **32–64 MB** (chunks or v3 shards) | throughput (saturate bandwidth across parallel workers) | chunk shape aligned with sampling pattern |
| **AI agentic** | ~1–16 MB | latency + request count (many small exploratory tool-mediated reads, tight budgets, limited retry patience) | schema enumerable in **1 request**; stable HTTPS URLs; self-describing names/units |

### The provider-agnostic wire target: ~8–16 MB compressed

Cross-provider request economics converge (AWS S3 documents 8–16 MB
byte-range requests; Azure ≥4 MiB blocks; GCS benchmarks knee around ≥1 MB;
ESIP/Pangeo geoscience guidance says ~10–100 MB): every GET pays a fixed
TTFB (tens of ms in-region) plus transfer at tens of MB/s per connection, so
the optimum is "big enough to amortize TTFB, small enough to parallelize and
retry cheaply." The bottom line: target **~8–16 MB compressed per
chunk/request** (acceptable ~4–64 MB, never below ~1 MB), leaning toward
16–64 MB when consumers are mostly remote/egress. These figures are **bytes
on the wire** — with typical 2–5× compression that is ~20–80 MB
uncompressed, which is why the training profile's 32–64 MB uncompressed
sweet spot and the agentic profile's 1–16 MB band are the same physics seen
from different sides. Pangeo's oft-quoted ~100 MB figure is *uncompressed
in-memory* Dask task sizing, not wire size — do not conflate them in
reports. Small chunks are additionally penalized by per-request pricing,
per-prefix rate limits, and scheduler overhead; very large chunks cost more
on partial reads, retries, and memory. The datacube guide
(https://developmentseed.org/datacube-guide/) catalogs the corresponding
worst practices.

### Shape alignment (training)

Chunk shape must match the sampling pattern, not just the size target:
- Vision-style spatial patch sampling ⇒ chunk **small in time, large in
  x/y** (e.g., `time=1..8, y=512, x=512`).
- Per-pixel / per-station time-series models ⇒ the inverse: **long in
  time, small in space** (e.g., `time=full-or-1000s, y=8..32, x=8..32`).
- Penalize orthogonal layouts explicitly. Canonical failure: global
  fields stored one-timestep-per-chunk when the use case is per-pixel
  time series — every series read touches every chunk in the archive.
- If the use case is unknown, score against the most plausible profile,
  state the assumption, and show the table for all three anyway.

### Agentic profile — the ≤3-request test

Score C3 by walking this path and counting requests:
1. catalog/root metadata → 2. variable/asset list → 3. one subset read.
Consolidated Zarr: 1+0+1 = 2 ✅. Unconsolidated Zarr v2: 1 + O(arrays) ❌.
COG: header(1) + tile(1) = 2 ✅. HDF5 scattered metadata: 10–50+ ❌.
Also verify URLs won't expire mid-session (signed URLs with short TTL
fail agentic even when fast).

## Sizing math (show your work in the report)

- Uncompressed chunk bytes = ∏(chunk_dims) × dtype.itemsize.
- Compressed ≈ uncompressed ÷ ratio. Measure the ratio from the one chunk
  decoded in the smoke test; if unmeasured, assume 2:1 and label the
  assumption.
- Total chunk count = Σ over arrays of ∏(ceil(shape_i / chunk_i)).
  **>~1M objects ⇒ recommend Zarr v3 sharding** (shard ≈ 64–256 MB outer,
  inner chunks per the profile). Millions of tiny objects break listings,
  syncs, and lifecycle management even when reads are fine.
- Expected request count for a canonical read pattern (put in the table):
  requests ≈ ⌈read_extent / chunk_extent⌉ per dimension, multiplied.
  Example: reading a 10-year daily time series at one pixel from
  `time=1, y=2048, x=2048` chunks ⇒ 3650 requests, each decompressing a
  full 2048×2048 field to yield one pixel; from `time=3650, y=32, x=32`
  chunks ⇒ 1 request.

## Codec guidance (C5)

Read-heavy AI workloads: favor fast-decode codecs — **ZSTD (level ≤~5),
LZ4, Blosc(lz4/zstd)**; DEFLATE acceptable but zstd dominates gzip/zlib on
every axis. Flag slow high-ratio settings (bzip2, LZMA, zstd level 19+) —
decode CPU becomes the bottleneck at training throughput. Lossy (JPEG/WebP)
only for visualization products. The smoke test empirically trials a codec
grid on one sampled chunk (`compression_trials` telemetry); interpret it
with `references/compression.md` — including the point that for float data,
precision filters (bit rounding) beat any codec change.

## Recommendation table template (must appear in every report)

| Profile | Current chunks (shape → ~MB) | Recommended (shape → ~MB) | Expected requests: canonical pattern (current → recommended) |
|---|---|---|---|
| Interactive | … | … | e.g., one 512² viewport tile: 1 → 1 |
| AI training | … | … | e.g., one 64 MB batch: 40 → 1–2 |
| AI agentic | … | … | catalog→vars→subset: 14 → 2 |

Fill with concrete numbers derived from observed shapes/dtypes; never
leave the table generic. When current layout already meets a profile,
say "keep as-is" in the recommended cell — the table still appears.


## Optional visual/analysis aids (mention in reports where useful)

- **vzviz** (https://github.com/virtual-zarr/vzviz) — visualize chunk grids
  of (virtual) Zarr stores; useful for showing a provider *why* a rechunk is
  recommended.
- **access-pattern-analysis** skill
  (https://www.skills.sh/uw-ssec/rse-plugins/access-pattern-analysis) —
  deeper assessment of whether a chunk shape fits specific access patterns;
  suggest it when the user has concrete query workloads beyond the three
  profiles.
