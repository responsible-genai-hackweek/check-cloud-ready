# Chunking guidance

This is the reasoning behind `chunking.py`'s measurement, grading, and orientation
logic — not a script to run. The CLI always measures; this doc explains what the
numbers mean.

## The size target, and why it generalizes

Every object-storage GET pays a fixed time-to-first-byte (tens of ms in-region, more
over the internet) plus transfer time at per-connection throughput on the order of
tens of MB/s. The optimal request size is "big enough that TTFB is amortized" (≥ a
few MB) and "small enough to parallelize, retry, and partially read cheaply" (≲ tens
of MB). Because latencies and per-connection throughput are similar across AWS, GCS,
and Azure, one recommendation holds everywhere: **target ~8–16 MB compressed per
chunk** (acceptable ~4–64 MB, never below ~1 MB).

- These numbers are bytes **on the wire** — compressed size. With typical 2–5×
  compression that's ~20–80 MB uncompressed. (Pangeo's older "~100 MB" figure is
  uncompressed in-memory Dask task sizing — a different thing.)
- Provider corroboration: S3 documents 8–16 MB byte-range requests and 5,500 GET/s
  per prefix; Azure wants blocks ≥ 4 MiB for high throughput; GCS benchmarks put the
  knee ≥ 1 MB.
- **In-region** consumers can lean toward the low end (~4–8 MB, TTFB already
  amortized well). **Egress/internet** consumers should lean high (~16–64 MB) —
  higher RTT needs more bytes per request to amortize.
- Too small: per-request overhead dominates, per-GET pricing and per-prefix rate
  limits bite, schedulers drown in tasks. Too big: partial reads waste transfer,
  retries are expensive, memory pressure rises, parallelism drops.

## ONE scored criterion: the measured compressed chunk size

`chunking.grade()` is the single scored band table, applied to the **sampled,
measured compressed** size of a chunk (never the theoretical uncompressed size —
`assess_chunking` always prefers a real measured size over an estimate, and labels
which one it used):

| Compressed chunk size | Grade |
|---|---|
| < 1 MB | **FAIL** — pathological for cloud access, per-request overhead dominates |
| 1–4 MB | **WARN** — below the wire-target sweet spot |
| 4–16 MB | **PASS** — within the wire-target sweet spot |
| 16–64 MB | **PASS** — fine, but lean smaller if reads are latency-sensitive (partial-read waste) |
| > 64 MB | **WARN** — large chunks tax partial reads and retries |

Sampling (`sample_chunk_sizes` → `pick_interior_chunks` + `is_data_bearing`): rather
than grabbing the first N stored chunks (which can be all-fill/all-NaN tile/grid
corners), the sampler walks outward from the array's centroid and rejects any
candidate whose decoded values are ≥90% fill/NaN, so the measured size reflects real
data. When measurement is impossible (no zarr/h5py, no stored chunks found, a
sampling error, or a contiguous/unchunked variable), a fallback estimate
(uncompressed bytes ÷ an assumed 2:1 ratio) is used and explicitly labeled
`estimated`, never blended silently with a measured value.

## Informational overlay: the three-profile table

Independently of the one scored grade above, every variable also gets a
**three-profile fit table** (`_build_profiles`) — informational at the `chunking.py`
level (no fail/warn is assigned here):

| Profile | Target (compressed) | Notes |
|---|---|---|
| Interactive / visualization | ~1–4 MB | latency-dominated; human waits per click |
| AI training (throughput) | ~10–100 MB, sweet spot 32–64 MB | throughput-dominated; shape should align with sampling pattern |
| AI agentic | ~1–16 MB | latency + request count; schema must also be enumerable in ≤3 requests |

Each profile's `status` is `within` / `below` / `above` the target band for the
variable's measured (or estimated) size. This overlay is what the **rubric**
(`references/rubric.md`, Dimension C's C1/C2/C3 checks) actually scores against per
profile — so while `chunking.py` itself treats the three profiles as descriptive
context alongside its one grade, the scoring layer above it does turn per-profile fit
into pass/partial/fail. Report all three regardless of which one the user cares
about; `--use-case` only affects which query gets its amplification *graded* (below),
not which profiles are shown.

## Orientation: what is this chunking optimized for?

`declare_orientation` classifies a variable's dims/shape/chunks as **map-optimized**
(time-thin, spatially large chunks — good for full-extent maps at one timestep, bad
for point time series), **timeseries-optimized** (the inverse), **balanced** (neither
extreme), or **tiled** (no time-like dimension at all — pure spatial tiling). It
computes the two canonical queries and their cost:

- **Full spatial extent at one time** (a "map" read) — chunks touched and bytes
  transferred vs. bytes actually needed.
- **Full-depth series at one point** (a "time series" read) — same.

**Read amplification** = bytes transferred ÷ bytes needed for that query. Reported as
evidence for both queries always; only *graded* when `--use-case
{timeseries|maps}` names which query is the real one:

- amplification > 10× → **WARN**
- amplification > 100× → **FAIL**

One chunk shape cannot serve both a map-heavy and a time-series-heavy audience well
at the same target size; when both patterns genuinely matter, a **dual-copy layout**
(one map-optimized store, one timeseries-optimized store) is a legitimate provider
recommendation — the tool says so explicitly in `balanced` orientation prose rather
than pretending one shape wins.

## Other production pitfalls

- **Tiny coordinate chunks.** A 1-D coordinate array split into many chunks forces a
  request per chunk before any real data flows, even though coordinates are normally
  read whole. `assess_chunking`'s coordinate-chunking notes flag this when a small
  (<8 MB) 1-D coordinate-like array is split into more than one chunk.
- **Dispersed metadata.** Opening a dataset should cost O(1) requests — Zarr
  consolidated metadata, or HDF5/NetCDF-4 metadata written contiguously. This is
  scored separately as the metadata-locality check (`references/rubric.md`
  Dimension B1), not by `chunking.py`.
- **Bloated datatypes.** float64 where float32 (or a scaled integer) carries the real
  information doubles every transfer — see `references/compression.md` on precision
  as the biggest lever.

## Tools

- **vzviz** (github.com/virtual-zarr/vzviz) visualizes chunk layout and simulates
  queries (read amplification, chunks touched, coalescing) for VirtualiZarr
  ManifestStores — useful for showing a provider *why* a rechunk is recommended.
- The **access-pattern-analysis** skill (uw-ssec/rse-plugins) formalizes access
  patterns from a user interview beyond the two canonical queries above.
