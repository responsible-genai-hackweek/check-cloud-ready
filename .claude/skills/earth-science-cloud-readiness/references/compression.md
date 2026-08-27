# Compression Evaluation

Read this when interpreting `compression_trials` telemetry or writing the
C5-codec finding and its remediation. The codec space has collapsed to a
shortlist — the interesting choices are the **filters** and the **evaluation
criteria**, not the compressor.

## The conventional shortlist

| Choice | When | Why |
|---|---|---|
| **zstd level 3** (+ byte shuffle for floats) | Default for array data on object storage; Zarr v3 default | Best ratio/speed balance. Levels above ~3 buy slightly better ratio at much longer write time, and — the key asymmetry — level barely affects *decompress* time, so the write-once/read-many argument caps out around 3–5, not 19. |
| **blosc-zstd** | When maximum ratio matters | Blosc wrapper adds blocking + built-in shuffle. |
| **blosc-lz4** | When decode speed is paramount (in-region, hot loops) | Worse ratio, much faster decode. |
| **gzip/zlib** | Only for NetCDF-4 compatibility | zstd beats it on every axis; flag as remediation target when seen. |
| lzma, bzip2 | Never for read-heavy archives | Decompression is slow, and that is the cost paid forever. |

Shuffle guidance: **byte shuffle on for floats** (meaningful ratio lift at
near-zero speed cost); bitshuffle tends to win for integer/label data;
no-shuffle sometimes wins for dense high-entropy data. A defensible default
with no evaluation at all: **zstd level 3 + shuffle**.

## The biggest lever is not the codec

For float geoscience data, lossless codecs plateau around **1.3–2.5×**
because trailing mantissa bits are effectively random. **Precision filters**
— bit rounding via `xbitinfo` (the Klöwer et al. "real information content"
approach, which reports how many mantissa bits per variable carry
information), or classic scale-offset/quantize — routinely add another
**2–5×** on top, dwarfing anything found by tuning zstd levels. If egress
cost motivates the assessment, point the provider here first: it is the only
knob with a scientific judgment call attached (how many bits to keep, per
variable). Always present lossy options as a provider decision with an error
column (max abs error, RMSE, visual/derivative check), never as a default.

## How the skill evaluates (and how to extend it)

`smoke_test.py` runs a **small grid, not an open search**, on one decoded
chunk per asset: `{zstd-1, zstd-3, zstd-5, blosc-lz4, blosc-zstd-3} ×
{shuffle, noshuffle}` via numcodecs, recording ratio, compress throughput,
and single-threaded decompress throughput. This costs zero network budget
(the chunk was already downloaded for the subset read). Caveats to state in
the report:

- One chunk of one variable is a **sample, not a survey** — ratio varies far
  more across variables/regions (land/ocean, seasons, levels) than across
  codecs. Recommend the provider re-run the grid on a representative chunk
  set before committing.
- Trials skip when the decoded sample is < 64 KB (not meaningful) and cap at
  16 MB (budget).

## Judging trials: the transfer model

Never rank codecs by ratio alone. Effective read time per chunk:

```
t_read ≈ TTFB + compressed_bytes / network_bandwidth
              + uncompressed_bytes / decompress_speed
```

- **Egress / cross-region** (tens of MB/s): the network term dominates →
  **ratio wins**, zstd is clearly right, and ratio directly multiplies
  egress dollar savings.
- **In-region** (multi-GB/s aggregate): decompression can become the
  bottleneck → lz4-class decode speed starts to justify its worse ratio.

When writing the C5 remediation, quote the measured ratio and decode speed
from the trials and say which regime the recommendation assumes.

## Sources

- Compression conventions and evaluation grid: HEFTIE Zarr benchmarks
  (https://heftieproject.github.io/zarr-benchmarks/), Cloud-Native
  Geospatial guide Zarr-in-practice
  (https://guide.cloudnativegeo.org/zarr/zarr-in-practice.html)
- Bit rounding / real information content: Klöwer et al. 2021, via xbitinfo
