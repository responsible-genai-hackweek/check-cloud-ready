#!/usr/bin/env python3
"""Benchmark the conventional codec grid on sampled chunks of a variable.

Usage:
  python3 compression_bench.py <url> --variable <name>
      [--engine zarr|hdf5] [--anon] [--n-chunks 3] [--keepbits N]
      [--credentials-url URL] [--granule-id G...-PROVIDER]

Grid: {zstd-1, zstd-3, zstd-5, blosc-lz4, blosc-zstd-3} x {shuffle, noshuffle},
plus optional bitround (lossy) when --keepbits is given.
Reports ratio, compress MB/s, decompress MB/s per configuration.
Prints JSON to stdout.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nasa_s3

_CLASSIFICATION_HINTS = {
    "credentials-endpoint-auth": (
        "EDL credentials missing/invalid — set EARTHDATA_TOKEN, "
        "EARTHDATA_USERNAME/EARTHDATA_PASSWORD, or ~/.netrc"),
    "in-region-only": (
        "credentials minted successfully; S3 denied from this network — "
        "expected outside us-west-2; re-run in-region"),
}


def note(msg):
    print(msg, file=sys.stderr)


def _classify_and_annotate(out, exc):
    """On a credentialed-path auth error, add classification + hint to
    `out` (mutated in place). Never rewrites to HTTPS."""
    classification = nasa_s3.classify_s3_error(exc)
    out["classification"] = classification
    hint = _CLASSIFICATION_HINTS.get(classification)
    if hint:
        out["hint"] = hint


def load_sample_chunks(url, variable, engine, anon, n_chunks,
                       credentials_url=None, granule_id=None):
    """Return a list of contiguous ndarray chunks spread across the array."""
    import numpy as np
    if url.startswith("s3://") and (credentials_url or granule_id):
        fs, path = nasa_s3.get_fs(url, anon=anon, credentials_url=credentials_url,
                                  granule_id=granule_id)
    else:
        import fsspec
        opts = {"anon": anon} if url.startswith("s3://") else {}
        fs, path = fsspec.core.url_to_fs(url, **opts)
    if engine == "zarr":
        import zarr
        g = zarr.open_group(fs.get_mapper(path), mode="r")
        arr = g[variable]
        chunks = arr.chunks
    else:
        import h5py
        f = fs.open(path, "rb")
        h = h5py.File(f, "r")
        arr = h[variable]
        chunks = arr.chunks or tuple(min(s, 256) for s in arr.shape)

    shape = arr.shape
    ndim = len(shape)
    n_per_axis = [max(1, s // c) for s, c in zip(shape, chunks)]
    samples = []
    for i in range(n_chunks):
        # spread sample chunks along the first axis (usually time), then middle
        idx = []
        for ax in range(ndim):
            if ax == 0 and n_per_axis[0] > 1:
                pos = (i * max(1, (n_per_axis[0] - 1) // max(1, n_chunks - 1))) \
                    if n_chunks > 1 else 0
                pos = min(pos, n_per_axis[0] - 1)
            else:
                pos = n_per_axis[ax] // 2
            start = pos * chunks[ax]
            idx.append(slice(start, min(start + chunks[ax], shape[ax])))
        data = np.asarray(arr[tuple(idx)])
        if data.size:
            samples.append(np.ascontiguousarray(data))
    return samples


def build_grid(keepbits, dtype):
    from numcodecs import Blosc, Zstd, Shuffle
    grid = []
    itemsize = dtype.itemsize
    for lvl in (1, 3, 5):
        grid.append((f"zstd-{lvl}+shuffle", [Shuffle(itemsize), Zstd(level=lvl)]))
        grid.append((f"zstd-{lvl}", [Zstd(level=lvl)]))
    grid.append(("blosc-lz4+shuffle",
                 [Blosc(cname="lz4", clevel=5, shuffle=Blosc.SHUFFLE)]))
    grid.append(("blosc-lz4", [Blosc(cname="lz4", clevel=5, shuffle=Blosc.NOSHUFFLE)]))
    grid.append(("blosc-zstd-3+shuffle",
                 [Blosc(cname="zstd", clevel=3, shuffle=Blosc.SHUFFLE)]))
    grid.append(("blosc-zstd-3",
                 [Blosc(cname="zstd", clevel=3, shuffle=Blosc.NOSHUFFLE)]))
    if keepbits is not None:
        try:
            from numcodecs import BitRound
            grid = [(f"bitround{keepbits}+" + name,
                     [BitRound(keepbits=keepbits)] + codecs)
                    for name, codecs in grid] + grid
        except ImportError:
            note("BitRound unavailable in this numcodecs; skipping lossy grid")
    return grid


def run_config(name, codecs, chunks_data):
    import numpy as np
    tot_raw = tot_comp = 0
    t_comp = t_decomp = 0.0
    err_max = None
    for data in chunks_data:
        raw = data.tobytes()
        tot_raw += len(raw)
        buf = data
        t0 = time.perf_counter()
        for c in codecs:
            buf = c.encode(buf)
        t_comp += time.perf_counter() - t0
        buf_bytes = buf if isinstance(buf, (bytes, bytearray)) else bytes(buf)
        tot_comp += len(buf_bytes)
        t0 = time.perf_counter()
        out = buf
        for c in reversed(codecs):
            out = c.decode(out)
        t_decomp += time.perf_counter() - t0
        if name.startswith("bitround"):
            dec = np.frombuffer(out, dtype=data.dtype)[:data.size] \
                if not isinstance(out, np.ndarray) else out
            err = float(np.nanmax(np.abs(
                dec.reshape(-1)[:data.size].astype("f8") -
                data.reshape(-1).astype("f8"))))
            err_max = max(err_max or 0.0, err)
    mb = tot_raw / 2**20
    res = {"ratio": round(tot_raw / tot_comp, 2) if tot_comp else None,
           "compress_MBps": round(mb / t_comp, 1) if t_comp else None,
           "decompress_MBps": round(mb / t_decomp, 1) if t_decomp else None,
           "compressed_MB_per_chunk": round(
               tot_comp / len(chunks_data) / 2**20, 3)}
    if err_max is not None:
        res["max_abs_error"] = err_max
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--variable", required=True)
    ap.add_argument("--engine", choices=["zarr", "hdf5"], default="zarr")
    ap.add_argument("--anon", action="store_true")
    ap.add_argument("--n-chunks", type=int, default=3)
    ap.add_argument("--keepbits", type=int, default=None,
                    help="also benchmark lossy bit-rounding with N kept bits")
    ap.add_argument("--credentials-url", help="DAAC s3credentials endpoint URL")
    ap.add_argument("--granule-id",
                    help="CMR granule concept ID, resolved via resolve_granule.py "
                         "(ignored if --credentials-url is also given)")
    a = ap.parse_args()
    credentialed = bool(a.credentials_url or a.granule_id)
    try:
        chunks_data = load_sample_chunks(a.url, a.variable, a.engine, a.anon,
                                         a.n_chunks, a.credentials_url,
                                         a.granule_id)
    except ImportError as e:
        sys.exit(f"Missing dependency: {e}. "
                 "pip install zarr h5py fsspec s3fs numcodecs zstandard numpy")
    except Exception as e:
        out = {"variable": a.variable, "error": f"{type(e).__name__}: {e}"}
        if credentialed:
            _classify_and_annotate(out, e)
        print(json.dumps(out, indent=2, default=str))
        return
    if not chunks_data:
        sys.exit("No data sampled - check variable name and URL")
    dtype = chunks_data[0].dtype
    note(f"sampled {len(chunks_data)} chunks of {a.variable}, dtype {dtype}, "
         f"{sum(c.nbytes for c in chunks_data)/2**20:.1f} MB uncompressed total")
    out = {"variable": a.variable, "dtype": str(dtype),
           "n_chunks_sampled": len(chunks_data),
           "uncompressed_chunk_mb": round(chunks_data[0].nbytes / 2**20, 3),
           "results": {}}
    for name, codecs in build_grid(a.keepbits, dtype):
        try:
            out["results"][name] = run_config(name, codecs, chunks_data)
        except Exception as e:
            out["results"][name] = {"error": f"{type(e).__name__}: {e}"}
    best = max((k for k, v in out["results"].items() if v.get("ratio")),
               key=lambda k: out["results"][k]["ratio"], default=None)
    out["best_ratio_config"] = best
    out["note"] = ("Judge with: read_time ~ TTFB + compressed/net_bw + "
                   "uncompressed/decompress_speed. Egress readers -> favor "
                   "ratio (zstd); in-region high-throughput -> favor decode "
                   "speed (lz4-class). See references/compression.md.")
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
