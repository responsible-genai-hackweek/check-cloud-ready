# Compression evaluation

This is the reasoning behind `compression.py`'s codec inspection and benchmark grid
— not a script to run. Evaluate by convention, not open-ended search: the codec
landscape has settled enough that a small grid answers the question.

## Two independent checks

1. **Codec inspection** (`inspect_codec`, always runs, no I/O, no soft dependency) —
   reads each variable's already-recorded codec string and judges it:
   - no codec / `"none"`/`"uncompressed"`/`""` → **fail**, "no compression detected".
   - `gzip`/`zlib`/`deflate` (any case) → **warn** — works, but zstd/blosc dominate it
     on every axis.
   - `zstd`/`blosc` (any case) → **pass** — modern, read-favoring codec.
   - anything else unrecognized → **warn**, "cannot judge automatically — review
     manually".
2. **Empirical benchmark** (`benchmark`, only with `--benchmark` and a decoded sample
   chunk) — an actual codec grid run on one real, data-bearing, interior-sampled
   chunk (see `references/chunking.md`'s sampling note).

## Codec shortlist

- **zstd** (Zarr v3 default) — the ratio workhorse.
- **Blosc wrappers** — blosc-zstd for ratio, blosc-lz4 for decode speed.
- **gzip/zlib** — only for NetCDF-4 ecosystem compatibility; if the dataset uses it,
  the benchmark quantifies the upgrade.
- **Skip lzma/bzip2** for read-heavy archives: slow decompression is paid by every
  reader forever.

## Settings that matter

- **zstd level ~3.** Higher levels buy slightly better ratio at much longer write
  time; level barely affects decompress (read) speed.
- **Byte shuffle ON for float data** — typical ratio lift ~1.5→1.9 at negligible
  cost. **Bitshuffle** often wins for integer/label data.
- **Zero-evaluation default: zstd level 3 + shuffle.** If the provider will only make
  one change, this is it.

## The current-codec baseline protocol

`benchmark`'s **first row always reproduces the dataset's own current codec** locally
(`_reproduce_current_codec`), so every comparison is against a real measured
baseline, not an assumption. It parses two different codec-string grammars —
HDF5's plain/hyphenated form (`"gzip"`, `"gzip+shuffle"`) and zarr's
`repr()`-style numcodecs/zarr-v3 strings (`"Blosc(cname='zstd', clevel=3,
shuffle=SHUFFLE, ...)"`, `"ZstdCodec(level=3, ...)"`) — to reconstruct the exact
codec+level+shuffle combination in use. If the current codec is too exotic to
reproduce locally, that row is a labeled placeholder (`"cannot reproduce locally"`)
and the standard grid still runs after it.

**Standard grid**: {zstd-1, zstd-3, zstd-5, blosc-lz4-5, blosc-zstd-3} × {shuffle,
noshuffle}, each row reporting compression ratio, compress MB/s, and single-thread
decompress MB/s.

## Judging with the end-to-end read model

**read time ≈ TTFB + compressed_bytes/network_bw + uncompressed_bytes/decompress_speed**

- **Egress/internet consumers** (tens of MB/s): the network term dominates → ratio
  wins → zstd.
- **In-region at multi-GB/s aggregate**: decompression can become the bottleneck →
  lz4-class speed can justify a worse ratio.
- Grade the current codec WARN if a shortlist codec beats it by >20% ratio *or* >2×
  decode speed at equal ratio; FAIL only for uncompressed floating-point data or a
  pathological choice (bzip2/lzma on a read-heavy archive).

## Lossy precision is a provider decision, never silent

Lossless compression plateaus at ~1.3–2.5× on floats because mantissa tails are
effectively random. **Bit rounding** (BitRound, per Klöwer et al.'s "real information
content" — keepbits per variable) buys another 2–5×, and is the single biggest lever
beyond codec choice — but it discards real precision, so:

- The benchmark **never runs a lossy trial unless asked.** `--bitround-max-abs-error`
  maps a user-supplied absolute-error budget to a `keepbits` value
  (`keepbits_for_max_abs_error`, an IEEE-754 mantissa-bits calculation against the
  sample's peak magnitude) and re-runs the entire grid with a `BitRound` pre-filter
  prepended to each row.
- Every lossy row is tagged `"lossy": true` and carries a measured `max_abs_error`
  against the original array — it is never merged into a lossless comparison
  silently; a caller/report must filter on `"lossy"` to separate the two.
- Recommend spot-checking derivatives/visualizations before a provider adopts a
  lossy setting. This is a scientific judgment call to surface, never a silent
  recommendation.

## Reading the C5-codec rubric check

`references/rubric.md`'s C5-codec dimension check reads **every** variable's codec
independently (not a single shared/top-level key) — a dataset with many variables
where only one happens to be recorded is not silently treated as "no compression
observed" for the whole file.
