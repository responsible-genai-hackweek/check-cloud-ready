#!/usr/bin/env python3
"""Report per-variable chunk geometry and sizes for a dataset.

Usage:
  python3 chunk_report.py <url> [--variables t2m,precip] [--anon]
                          [--engine zarr|h5netcdf|netcdf4] [--samples 8]

For each variable: dtype, shape, chunk shape, chunk count, uncompressed chunk
size, SAMPLED compressed chunk size (median of N stored chunks), codec pipeline.
Grades sizes against the 8-16 MB compressed target (accept 4-64 MB, floor 1 MB).
Prints JSON to stdout.
"""
import argparse
import json
import statistics
import sys


def note(msg):
    print(msg, file=sys.stderr)


def grade(compressed_mb):
    if compressed_mb is None:
        return "unknown"
    if compressed_mb < 1:
        return "FAIL (<1 MB)"
    if compressed_mb < 4:
        return "WARN (1-4 MB)"
    if compressed_mb <= 16:
        return "PASS"
    if compressed_mb <= 64:
        return "PASS (16-64 MB; fine, watch partial-read waste)"
    return "WARN (>64 MB)"


def zarr_report(url, variables, anon, samples):
    import zarr
    import fsspec
    opts = {"anon": anon} if url.startswith("s3://") else {}
    fs, path = fsspec.core.url_to_fs(url, **opts)
    store = zarr.open_group(url, mode="r",
                            storage_options=opts if opts else None)
    out = {"reader": "zarr", "zarr_format": getattr(store, "metadata", None)
           and getattr(store.metadata, "zarr_format", None) or 2}
    arrays = {}
    for name, arr in store.arrays(recurse=True) if hasattr(store, "arrays") \
            else [(k, store[k]) for k in store.array_keys()]:
        arrays[name] = arr
    if variables:
        arrays = {k: v for k, v in arrays.items()
                  if k.split("/")[-1] in variables or k in variables}
    out["variables"] = {}
    for name, arr in arrays.items():
        v = describe_array(name, arr.dtype, arr.shape, arr.chunks,
                           codec=str(getattr(arr, "compressors", None) or
                                     getattr(arr, "compressor", None)) +
                           " filters=" + str(getattr(arr, "filters", None)))
        # sample stored chunk sizes
        try:
            sizes = sample_zarr_chunk_sizes(fs, path, name, arr, samples)
            if sizes:
                v["compressed_chunk_mb_median"] = round(
                    statistics.median(sizes) / 2**20, 3)
                v["compressed_chunk_mb_range"] = [
                    round(min(sizes) / 2**20, 3), round(max(sizes) / 2**20, 3)]
                v["chunks_sampled"] = len(sizes)
        except Exception as e:
            v["compressed_sample_error"] = f"{type(e).__name__}: {e}"
        v["size_grade"] = grade(v.get("compressed_chunk_mb_median"))
        out["variables"][name] = v
    return out


def sample_zarr_chunk_sizes(fs, path, name, arr, samples):
    """List a few chunk objects and take their stored sizes."""
    import itertools
    prefix = f"{path.rstrip('/')}/{name}/"
    sizes = []
    listed = fs.find(prefix, maxdepth=None, detail=True)
    metadata_names = {".zarray", ".zattrs", "zarr.json"}
    items = [(k, i) for k, i in listed.items()
             if k.split("/")[-1] not in metadata_names]
    for k, info in itertools.islice(items, samples):
        s = info.get("size")
        if s:
            sizes.append(s)
    return sizes


def hdf5_report(url, variables, anon, samples):
    import h5py
    import fsspec
    opts = {"anon": anon} if url.startswith("s3://") else {}
    f = fsspec.open(url, "rb", **opts).open()
    h = h5py.File(f, "r")
    out = {"reader": "h5py", "variables": {}}
    names = []
    h.visit(lambda n: names.append(n))
    dsets = [n for n in names if isinstance(h[n], h5py.Dataset)]
    if variables:
        dsets = [n for n in dsets
                 if n.split("/")[-1] in variables or n in variables]
    for name in dsets:
        d = h[name]
        codec = f"compression={d.compression}"
        if d.compression_opts is not None:
            codec += f"({d.compression_opts})"
        if d.shuffle:
            codec += "+shuffle"
        v = describe_array(name, d.dtype, d.shape, d.chunks, codec=codec)
        if d.chunks is None:
            v["note"] = ("contiguous (unchunked) - whole-variable reads only; "
                         "no subsetting benefit from compression" if d.compression
                         else "contiguous (unchunked) - strided range reads possible "
                              "but no chunk-level parallelism")
        else:
            try:
                dsid = d.id
                n = dsid.get_num_chunks()
                v["stored_chunks"] = n
                sizes = []
                step = max(1, n // samples)
                for i in range(0, n, step):
                    sizes.append(dsid.get_chunk_info(i).size)
                    if len(sizes) >= samples:
                        break
                if sizes:
                    v["compressed_chunk_mb_median"] = round(
                        statistics.median(sizes) / 2**20, 3)
                    v["compressed_chunk_mb_range"] = [
                        round(min(sizes) / 2**20, 3),
                        round(max(sizes) / 2**20, 3)]
                    v["chunks_sampled"] = len(sizes)
            except Exception as e:
                v["compressed_sample_error"] = f"{type(e).__name__}: {e}"
        v["size_grade"] = grade(v.get("compressed_chunk_mb_median"))
        out["variables"][name] = v
    return out


def describe_array(name, dtype, shape, chunks, codec=None):
    import math
    itemsize = dtype.itemsize if hasattr(dtype, "itemsize") else 8
    v = {"dtype": str(dtype), "shape": list(shape)}
    if chunks:
        v["chunk_shape"] = list(chunks)
        n_elem = math.prod(chunks)
        v["uncompressed_chunk_mb"] = round(n_elem * itemsize / 2**20, 3)
        v["n_chunks"] = math.prod(
            math.ceil(s / c) for s, c in zip(shape, chunks)) if shape else 1
    else:
        v["chunk_shape"] = None
        v["uncompressed_total_mb"] = round(
            math.prod(shape) * itemsize / 2**20, 3) if shape else 0
    if codec:
        v["codec"] = codec
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--variables", help="comma-separated variable names")
    ap.add_argument("--anon", action="store_true")
    ap.add_argument("--samples", type=int, default=8,
                    help="stored chunks to sample per variable")
    ap.add_argument("--engine", choices=["auto", "zarr", "hdf5"], default="auto")
    a = ap.parse_args()
    variables = a.variables.split(",") if a.variables else None

    engine = a.engine
    if engine == "auto":
        engine = "zarr" if a.url.rstrip("/").endswith((".zarr", "/")) or \
            "zarr" in a.url else "hdf5"
        note(f"engine auto-selected: {engine} (override with --engine)")
    try:
        if engine == "zarr":
            out = zarr_report(a.url, variables, a.anon, a.samples)
        else:
            out = hdf5_report(a.url, variables, a.anon, a.samples)
    except ImportError as e:
        sys.exit(f"Missing dependency: {e}. pip install zarr h5py fsspec s3fs")
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}",
               "hint": "If auto-detection picked the wrong reader, pass "
                       "--engine zarr|hdf5. NetCDF-3 files have no chunks - "
                       "report that directly rather than running this script."}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
