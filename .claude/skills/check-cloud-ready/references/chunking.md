# Chunking guidance

## The size target, and why it generalizes

Every object-storage GET pays a fixed time-to-first-byte (tens of ms in-region,
more over the internet) plus transfer time at per-connection throughput on the
order of tens of MB/s. The optimal request size is simply "big enough that TTFB is
amortized" (≥ a few MB) and "small enough to parallelize, retry, and partially read
cheaply" (≲ tens of MB). Because latencies and per-connection throughput are
similar across AWS, GCS, and Azure, one recommendation holds everywhere:

**Target ~8–16 MB *compressed* per chunk. Acceptable range ~4–64 MB. Never below
~1 MB.**

- These numbers are bytes **on the wire** — compressed size. With typical 2–5×
  compression that's ~20–80 MB uncompressed. (Pangeo's older "~100 MB" figure is
  uncompressed in-memory Dask task sizing — don't confuse the two.)
- Provider corroboration: S3 documents 8–16 MB byte-range requests and 5,500
  GET/s per prefix; Azure wants blocks ≥ 4 MiB for high-throughput; GCS
  benchmarks put the knee ≥ 1 MB.
- **In-region** consumers: the low end (~4–8 MB) already amortizes TTFB well.
  **Egress/internet** consumers: lean high (~16–64 MB) — higher RTT needs more
  bytes per request to amortize, with concurrency providing aggregate throughput.
- Why not smaller: per-request overhead dominates, request pricing (~$0.0004/1k
  GETs on S3) and per-prefix rate limits bite, and schedulers drown in tasks.
- Why not bigger: partial reads waste transfer, retries are expensive, memory
  pressure per worker, and less parallelism.

## Grading

| Compressed chunk size | Grade |
|---|---|
| 8–16 MB | PASS |
| 4–8 or 16–64 MB | PASS (note the direction to lean given the audience) |
| 1–4 MB | WARN — works, but request overhead is a measurable tax |
| < 1 MB | FAIL — pathological for cloud access |
| > 64 MB | WARN/FAIL — partial-read waste; FAIL if a common query reads a small fraction of each chunk |

Grade on the *sampled compressed* size, not the theoretical uncompressed size.
Report per-variable medians; call out outlier variables separately.

## Beyond size: the other production pitfalls

From the Development Seed datacube guide's "worst practices" — check each:

- **Tiny coordinate chunks.** Readers fetch all coordinates before doing anything.
  A time coordinate chunked 1-per-chunk across 10,000 steps = 10,000 GETs to plot
  one map. Coordinates should be readable in one request each (single chunk, or
  consolidated/inlined).
- **Dispersed metadata.** Opening the dataset should cost O(1) requests: Zarr →
  consolidated metadata (`.zmetadata` / v3 consolidated) present; NetCDF-4/HDF5 →
  metadata written contiguously (`h5repack` with a paged aggregation strategy, or
  files written with recent netCDF defaults). A file that takes hundreds of reads
  to open fails the spirit of random access even if ranges work.
- **Non-standardized metadata** → covered by the CF/GeoZarr checks.
- **Bloated datatypes.** float64 where float32 (or a scaled integer) carries the
  real information doubles every transfer. Flag when dtype precision visibly
  exceeds the physical measurement precision.

## Chunk shape (only when the user states target use cases)

Size says how much per request; shape says *which* bytes end up in each request.
For each stated access pattern, estimate:

- **Chunks touched** by a representative query, and
- **Read amplification** = bytes transferred ÷ bytes actually needed.

Rules of thumb for a (time, y, x[, level]) cube:

- Map-per-timestep queries want spatially-large, time-thin chunks.
- Point/regional time series want time-deep chunks (many steps per chunk).
- One shape cannot serve both extremes well at 8–16 MB; balanced 3-D chunks
  (e.g. moderate in all axes) are the usual compromise, and *dual copies*
  (a time-optimized and a space-optimized store) are a legitimate provider
  recommendation when both patterns matter — say so rather than pretending one
  shape wins.
- Read amplification > ~10× for a primary stated use case → WARN; > ~100× → FAIL.

Tools: the `access-pattern-analysis` skill (uw-ssec/rse-plugins) formalizes
patterns from a user interview; `vzviz` (pip install from
github.com/virtual-zarr/vzviz) visualizes chunk layout and simulates queries
(read amplification, chunks touched, coalescing) for VirtualiZarr ManifestStores.
