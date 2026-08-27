#!/usr/bin/env python3
"""Identify a dataset's format from magic bytes and store layout.

Usage: python3 detect_format.py <url_or_path> [--anon]

Distinguishes: HDF4, HDF5, NetCDF-3 (classic/64-bit/CDF-5), NetCDF-4,
Zarr v2, Zarr v3, Icechunk, Kerchunk/VirtualiZarr reference JSON or parquet.
Prints JSON to stdout.
"""
import argparse
import json
import sys

MAGIC = [
    (b"\x89HDF\r\n\x1a\n", "HDF5"),
    (b"CDF\x01", "NetCDF-3 classic"),
    (b"CDF\x02", "NetCDF-3 64-bit offset"),
    (b"CDF\x05", "NetCDF-3 64-bit data (CDF-5)"),
    (b"\x0e\x03\x13\x01", "HDF4"),
]
HDF5_OFFSETS = [0, 512, 1024, 2048, 4096]  # superblock may follow a user block


class _LocalFS:
    """Minimal stdlib fallback so local paths work without fsspec."""

    def open(self, path, mode="rb"):
        return open(path, mode)

    def exists(self, path):
        import os
        return os.path.exists(path)

    def isdir(self, path):
        import os
        return os.path.isdir(path)


def get_fs(url, anon):
    if "://" not in url:
        return _LocalFS(), url
    try:
        import fsspec
    except ImportError:
        sys.exit("Missing dependency for remote URLs: "
                 "pip install fsspec s3fs requests aiohttp")
    opts = {}
    if url.startswith("s3://"):
        opts["anon"] = anon
    fs, path = fsspec.core.url_to_fs(url, **opts)
    return fs, path


def read_bytes(fs, path, start, length):
    with fs.open(path, "rb") as f:
        f.seek(start)
        return f.read(length)


def sniff_file(fs, path, out):
    head = read_bytes(fs, path, 0, 8)
    out["first_bytes_hex"] = head.hex()
    for magic, name in MAGIC:
        if head.startswith(magic):
            out["format"] = name
            break
    else:
        # HDF5 signature after a user block?
        for off in HDF5_OFFSETS[1:]:
            try:
                if read_bytes(fs, path, off, 8) == b"\x89HDF\r\n\x1a\n":
                    out["format"] = "HDF5"
                    out["hdf5_superblock_offset"] = off
                    break
            except Exception:
                break
        else:
            if head[:1] in (b"{", b"["):
                blob = head + read_bytes(fs, path, 8, 4088)
                if b'"refs"' in blob or b'"version"' in blob and b'"templates"' in blob:
                    out["format"] = "Kerchunk/VirtualiZarr reference JSON"
                else:
                    out["format"] = "JSON (unrecognized - possibly a reference file; inspect keys)"
            elif head.startswith(b"PAR1"):
                out["format"] = "Parquet (possibly kerchunk parquet references or GeoParquet)"
            elif head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
                out["format"] = "TIFF (possibly COG - not yet supported by this skill)"
            elif head.startswith(b"GRIB"):
                out["format"] = "GRIB (not yet supported by this skill)"
            else:
                out["format"] = "unrecognized"
    if out.get("format") == "HDF5":
        out.update(refine_hdf5(fs, path))
    return out


def refine_hdf5(fs, path):
    """Is this HDF5 actually NetCDF-4?"""
    try:
        import h5py
    except ImportError:
        return {"netcdf4_check": "skipped (pip install h5py to distinguish "
                                 "NetCDF-4 from plain HDF5)"}
    try:
        with fs.open(path, "rb") as f, h5py.File(f, "r") as h:
            if "_NCProperties" in h.attrs:
                return {"format": "NetCDF-4 (HDF5-based)",
                        "nc_properties": h.attrs["_NCProperties"].decode()
                        if isinstance(h.attrs["_NCProperties"], bytes)
                        else str(h.attrs["_NCProperties"])}
            # dimension scales are a strong netCDF-4 signal
            for _, obj in h.items():
                if hasattr(obj, "attrs") and "CLASS" in obj.attrs:
                    cls = obj.attrs["CLASS"]
                    cls = cls.decode() if isinstance(cls, bytes) else str(cls)
                    if cls == "DIMENSION_SCALE":
                        return {"format": "NetCDF-4 (HDF5-based, no _NCProperties)"}
            return {"format": "HDF5 (no netCDF-4 markers found)"}
    except Exception as e:
        return {"netcdf4_check": f"failed: {type(e).__name__}: {e}"}


def sniff_store(fs, path, out):
    """Zarr v2/v3 / Icechunk store layouts."""
    def exists(k):
        try:
            return fs.exists(f"{path.rstrip('/')}/{k}")
        except Exception:
            return False

    if exists("zarr.json"):
        out["format"] = "Zarr v3"
        out["consolidated_metadata"] = "check zarr.json for consolidated_metadata key"
    elif exists(".zmetadata"):
        out["format"] = "Zarr v2"
        out["consolidated_metadata"] = True
    elif exists(".zgroup") or exists(".zarray"):
        out["format"] = "Zarr v2"
        out["consolidated_metadata"] = False
        out["note"] = ("No .zmetadata - readers must crawl the store to "
                       "discover arrays (a metadata-dispersal finding).")
    elif exists("refs") and (exists("snapshots") or exists("manifests")):
        out["format"] = "Icechunk repository"
    else:
        return None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--anon", action="store_true", default=None,
                    help="force anonymous S3 access")
    a = ap.parse_args()
    anon = True if a.anon else False
    out = {"url": a.url}
    fs, path = get_fs(a.url, anon)

    isdir = False
    try:
        isdir = fs.isdir(path)
    except Exception:
        pass
    looks_like_store = isdir or a.url.rstrip("/").endswith((".zarr", ".icechunk"))

    try:
        if looks_like_store and sniff_store(fs, path, dict(out)) is not None:
            out = sniff_store(fs, path, out)
        else:
            out = sniff_file(fs, path, out)
    except Exception as e:
        # Maybe it's a store after all (file open on a prefix fails)
        store = sniff_store(fs, path, out)
        if store is None:
            out["error"] = f"{type(e).__name__}: {e}"
        else:
            out = store
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
