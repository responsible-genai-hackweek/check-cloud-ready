# Compression evaluation

Evaluate by convention, not open-ended search. The codec landscape has settled
enough that a small grid answers the question.

## Codec shortlist

- **zstd** (Zarr v3 default) — the ratio workhorse.
- **Blosc wrappers** — blosc-zstd for ratio, blosc-lz4 for decode speed.
- **gzip/zlib** — only for NetCDF-4 ecosystem compatibility (it's what classic
  netCDF tooling reads everywhere); if the dataset uses it, benchmark the upgrade.
- **Skip lzma/bzip2** for read-heavy archives: slow decompression is paid by every
  reader forever.

## Settings that matter

- **zstd level ~3.** Higher levels buy slightly better ratio at much longer write
  time; level barely affects decompress (read) speed, so there's little reader-side
  reason to crank it.
- **Byte shuffle ON for float data** — typical ratio lift ~1.5→1.9 at negligible
  cost. **Bitshuffle** often wins for integer/label data. No-shuffle occasionally
  wins on dense high-entropy data.
- **Zero-evaluation default: zstd level 3 + shuffle.** If the provider will only
  make one change, this is it.

## The biggest lever is precision, not codec

Lossless compression plateaus at ~1.3–2.5× on floats because mantissa tails are
effectively random. **Bit rounding** (xbitinfo, per Klöwer et al.'s "real
information content" — keepbits per variable) or scale-offset/quantization buys
another 2–5×. This is the only knob requiring a scientific judgment call (how many
bits are real), so: benchmark it if the user allows lossy, present the numbers, and
frame it as a decision for the provider — never silently recommend discarding
precision.

## Benchmark protocol (what `compression_bench.py` implements)

1. Sample representative chunks *at the target chunk size* — vary variable, level,
   season, land/ocean where the data allows. Ratio varies more across data content
   than across codecs, so a single-chunk benchmark misleads.
2. Run the grid: {zstd-1, zstd-3, zstd-5, blosc-lz4, blosc-zstd-3} ×
   {shuffle, noshuffle}, plus the dataset's current codec as baseline.
   (× bitround keepbits when lossy is allowed.)
3. Record: compression ratio, compress MB/s, single-thread decompress MB/s.
4. Judge with the end-to-end read model:
   **read time ≈ TTFB + compressed_bytes/network_bw + uncompressed_bytes/decompress_speed**

## Decision rules

- **Egress/internet consumers** (tens of MB/s): the network term dominates → ratio
  wins → zstd. Ratio also directly multiplies egress cost savings.
- **In-region at multi-GB/s aggregate**: decompression can become the bottleneck →
  lz4-class speed can justify a worse ratio.
- Report the current codec's position in the grid. Grade WARN if a shortlist codec
  beats it by >20% ratio *or* >2× decode speed at equal ratio; FAIL only for
  uncompressed floating-point data or pathological choices (bzip2/lzma on a
  read-heavy archive).
- If lossy was benchmarked, report error columns alongside (max abs error, RMSE)
  and suggest spot-checking derivatives/visualizations before adoption.
