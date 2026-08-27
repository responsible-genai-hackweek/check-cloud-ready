#!/usr/bin/env python3
"""Bounded, instrumented smoke tests for cloud-readiness assessment.

Per asset, with hard caps (~25 MB total transfer, 60 s):
  1. HEAD probe (status, Content-Length, Accept-Ranges, ETag, TLS)
  2. Ranged reads (first 16 KB + one interior range; verify 206 + byte count)
  3. Instrumented lazy open (count HTTP requests + bytes to open)
  4. Tiny subset read (one chunk/tile/row group; TTFB, throughput)
  5. Format validation (rio-cogeo, consolidated metadata, h5py chunk
     introspection, parquet footer stats)
  6. Graceful degradation: network blocked / auth failure => SKIPPED, not
     FAILED, with a reason; report confidence is downgraded, not the score.

All probes are ranged and byte-capped; whole files are never downloaded.

NASA Earthdata S3 (`s3://` on a protected DAAC bucket) is read with
credentials minted from the DAAC's `/s3credentials` endpoint (see
`nasa_s3.py`); it is never rerouted to a public HTTPS URL. Without a
credentials endpoint (or a CMR granule ID that resolves one) the asset is
SKIPPED, never FAILED.

Usage (standalone):
    python smoke_test.py <url> [--format cog|zarr|hdf5|parquet|...]
                               [--credentials-url URL | --granule-id G...-PROV]
                               [--earthaccess-fallback]
Importable API:
    run_smoke_test(url, fmt, budget=None) -> dict
"""

from __future__ import annotations

import json
import os
import sys
import time
import argparse
from urllib.parse import urlparse
import builtins

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nasa_s3  # noqa: E402
import resolve_granule  # noqa: E402


# ---------------------------------------------------------------- optional deps
def _try(name):
    try:
        return __import__(name)
    except Exception:
        return None

httpx = _try("httpx")
fsspec = _try("fsspec")
rasterio = _try("rasterio")
zarr = _try("zarr")
xr = _try("xarray")
h5py = _try("h5py")
pa = _try("pyarrow")
numcodecs = _try("numcodecs")


def _np_prod(seq):
    p = 1
    for x in seq:
        p *= int(x)
    return p


def _trial_codecs(arr, max_mb=16):
    """Empirical compression grid on one decoded chunk (numpy array).
    Grid per compression-guidance conventions: {zstd-1,3,5, blosc-lz4,
    blosc-zstd-3} x {shuffle, noshuffle}. Records ratio + compress and
    single-threaded decompress throughput. Purely local CPU work — costs
    zero network budget. Returns [] if numcodecs/numpy unavailable or the
    sample is too small to be meaningful (<64 KB)."""
    if numcodecs is None:
        return []
    try:
        import numpy as np
        a = np.ascontiguousarray(arr)
        if a.nbytes < 64 * 1024:
            return []
        if a.nbytes > max_mb * 1024 * 1024:  # trial on a slice, stay cheap
            a = a.reshape(-1)[: (max_mb * 1024 * 1024) // max(a.itemsize, 1)]
        raw = a.tobytes()
        itemsize = a.itemsize
        grid = [("zstd-1", numcodecs.Zstd(level=1)),
                ("zstd-3", numcodecs.Zstd(level=3)),
                ("zstd-5", numcodecs.Zstd(level=5)),
                ("blosc-lz4", numcodecs.Blosc(cname="lz4", clevel=5,
                                              shuffle=numcodecs.Blosc.NOSHUFFLE)),
                ("blosc-zstd-3", numcodecs.Blosc(cname="zstd", clevel=3,
                                                 shuffle=numcodecs.Blosc.NOSHUFFLE))]
        out = []
        for shuffle in (False, True):
            buf = raw
            if shuffle:
                try:
                    buf = numcodecs.Shuffle(elementsize=itemsize).encode(raw)
                    buf = bytes(buf)
                except Exception:
                    continue
            for name, codec in grid:
                try:
                    t0 = time.monotonic()
                    comp = codec.encode(buf)
                    t_c = time.monotonic() - t0
                    t1 = time.monotonic()
                    codec.decode(comp)
                    t_d = time.monotonic() - t1
                    out.append({
                        "codec": name + ("+shuffle" if shuffle else ""),
                        "ratio": round(len(raw) / max(len(comp), 1), 2),
                        "compress_MBps": round((len(raw) / 1e6) / t_c, 1) if t_c > 0 else None,
                        "decompress_MBps": round((len(raw) / 1e6) / t_d, 1) if t_d > 0 else None,
                    })
                except Exception:
                    continue
        out.sort(key=lambda r: -r["ratio"])
        return out
    except Exception:
        return []
if pa is not None:
    try:
        import pyarrow.parquet as papq  # noqa: F401
    except Exception:
        papq = None
else:
    papq = None
try:
    from rio_cogeo.cogeo import cog_validate  # type: ignore
except Exception:
    cog_validate = None

BYTE_CAP = 25 * 1024 * 1024   # ~25 MB per asset
TIME_CAP = 60.0               # seconds per asset
HEADER_READ = 16 * 1024       # 16 KB


class BudgetExceeded(Exception):
    pass


class Budget:
    """Tracks bytes transferred and wall time for one asset."""

    def __init__(self, byte_cap=BYTE_CAP, time_cap=TIME_CAP):
        self.byte_cap = byte_cap
        self.time_cap = time_cap
        self.bytes = 0
        self.requests = 0
        self.t0 = time.monotonic()

    def spend(self, nbytes, nreq=1):
        self.bytes += int(nbytes)
        self.requests += int(nreq)
        if self.bytes > self.byte_cap:
            raise BudgetExceeded(f"byte cap {self.byte_cap} exceeded ({self.bytes})")
        if time.monotonic() - self.t0 > self.time_cap:
            raise BudgetExceeded(f"time cap {self.time_cap}s exceeded")

    def snapshot(self):
        return {"bytes": self.bytes, "requests": self.requests,
                "elapsed_s": round(time.monotonic() - self.t0, 3)}


# -------------------------------------------------------------- skip reasons
# Auth/region problems are always SKIPPED (never FAILED): the dataset is not
# graded down, the report's confidence label is.
NASA_CREDENTIALS_REQUIRED_REASON = (
    "nasa-credentials-required: pass --granule-id or --credentials-url "
    "(recommended), or --earthaccess-fallback")
IN_REGION_ONLY_REASON = (
    "in-region-only: credentials minted successfully but S3 denied from this "
    "network (expected outside us-west-2)")
CREDENTIALS_AUTH_REASON = (
    "auth: EDL credentials missing/invalid (check EARTHDATA_TOKEN / "
    "EARTHDATA_USERNAME+PASSWORD / ~/.netrc)")

_AUTH_SKIP_REASONS = {
    "in-region-only": IN_REGION_ONLY_REASON,
    "credentials-endpoint-auth": CREDENTIALS_AUTH_REASON,
}


def _s3_auth_skip_reason(exc):
    """Map an exception raised on the credentialed S3 path to a SKIPPED
    reason string, or None if it isn't an auth/region problem."""
    return _AUTH_SKIP_REASONS.get(nasa_s3.classify_s3_error(exc))


# ---------------------------------------------------------------- HTTP probes
# Authenticated NASA filesystems, keyed by credentials endpoint. nasa_s3.get_fs
# stays pure; this cache just avoids re-minting credentials per asset.
_AUTH_FS_CACHE: dict = {}


def _to_https(url: str) -> str:
    """Best-effort conversion of s3://, gs://, az:// to a probe-able HTTPS URL.
    Region-specific endpoints may be needed; failures are reported, not fatal.
    NEVER used for NASA Earthdata s3:// URLs — those are read through the
    credentialed S3 path or skipped."""
    p = urlparse(url)
    if p.scheme == "s3":
        return f"https://{p.netloc}.s3.amazonaws.com{p.path}"
    if p.scheme == "gs":
        return f"https://storage.googleapis.com/{p.netloc}{p.path}"
    if p.scheme == "az":
        return url  # cannot infer account endpoint; caller should supply https
    return url


def head_probe(url: str, budget: Budget, headers=None) -> dict:
    out = {"check": "HEAD", "url": url}
    if httpx is None:
        out.update(status="skipped", reason="httpx not installed")
        return out
    try:
        with httpx.Client(follow_redirects=True, timeout=15, verify=True) as c:
            t0 = time.monotonic()
            r = c.head(url, headers=headers or None)
            budget.spend(0, 1)
            out.update(
                status="pass" if r.status_code < 400 else "fail",
                http_status=r.status_code,
                content_length=r.headers.get("Content-Length"),
                accept_ranges=r.headers.get("Accept-Ranges"),
                etag=r.headers.get("ETag"),
                content_type=r.headers.get("Content-Type"),
                cors_allow_origin=r.headers.get("Access-Control-Allow-Origin"),
                redirects=len(r.history),
                final_url=str(r.url),
                tls_ok=str(r.url).startswith("https://"),
                latency_ms=round((time.monotonic() - t0) * 1000, 1),
            )
            if r.status_code in (401, 403):
                out.update(status="skipped", reason="auth")
    except BudgetExceeded:
        raise
    except Exception as e:
        out.update(status="skipped", reason=f"network: {type(e).__name__}: {e}")
    return out


def _probe_ranges(content_length=None):
    """First 16 KB + (if the object is big enough) one interior range."""
    ranges = [(0, HEADER_READ - 1)]
    try:
        cl = int(content_length) if content_length else None
    except (TypeError, ValueError):
        cl = None
    if cl and cl > HEADER_READ * 4:
        mid = cl // 2
        ranges.append((mid, min(mid + HEADER_READ - 1, cl - 1)))
    return ranges


def ranged_probe(url: str, budget: Budget, content_length=None,
                 headers=None) -> dict:
    """First 16 KB + one interior range; verify 206 + correct byte counts."""
    out = {"check": "ranged_reads", "reads": []}
    if httpx is None:
        out.update(status="skipped", reason="httpx not installed")
        return out
    ranges = _probe_ranges(content_length)
    status = "pass"
    first_bytes = b""
    try:
        with httpx.Client(follow_redirects=True, timeout=15) as c:
            for (a, b) in ranges:
                t0 = time.monotonic()
                r = c.get(url, headers={**(headers or {}),
                                        "Range": f"bytes={a}-{b}"})
                got = len(r.content)
                budget.spend(got, 1)
                ok = r.status_code == 206 and got == (b - a + 1)
                if r.status_code == 200:
                    # server ignored Range — dangerous (whole-file). content
                    # already limited by httpx read; mark failure.
                    ok = False
                out["reads"].append({
                    "range": f"{a}-{b}", "http_status": r.status_code,
                    "bytes": got, "expected": b - a + 1, "ok": ok,
                    "latency_ms": round((time.monotonic() - t0) * 1000, 1)})
                if not ok:
                    status = "fail"
                if a == 0:
                    first_bytes = r.content[:64]
        out["status"] = status
        out["first_bytes_hex"] = first_bytes[:16].hex()
        out["first_bytes"] = first_bytes
    except BudgetExceeded:
        raise
    except Exception as e:
        out.update(status="skipped", reason=f"network: {type(e).__name__}: {e}")
    return out


# ------------------------------------------- authenticated S3 probes (no HTTPS)
def fs_head_probe(cfs, path: str, budget: Budget) -> dict:
    """HEAD-equivalent over an authenticated filesystem (`fs.info`). Same
    telemetry field names as head_probe so assess.py needs no special case.
    Raises on auth/region failure; the caller classifies it."""
    out = {"check": "HEAD", "url": path, "transport": "s3-authenticated"}
    t0 = time.monotonic()
    info = cfs.info(path) or {}
    size = info.get("size", info.get("Size", info.get("ContentLength")))
    etag = info.get("ETag", info.get("etag", info.get("e_tag")))
    out.update(
        status="pass",
        content_length=size,
        # S3 GetObject always honours Range; TLS is enforced by the SDK.
        accept_ranges="bytes",
        etag=etag,
        content_type=info.get("ContentType", info.get("type")),
        cors_allow_origin=None,
        redirects=0,
        final_url=path,
        tls_ok=True,
        latency_ms=round((time.monotonic() - t0) * 1000, 1),
    )
    return out


def fs_ranged_probe(cfs, path: str, budget: Budget, content_length=None) -> dict:
    """Ranged reads over an authenticated filesystem (`fs.cat_file`). Emits
    the same field names as ranged_probe (`reads`, `first_bytes_hex`,
    `first_bytes`) so format sniffing works identically."""
    out = {"check": "ranged_reads", "reads": [], "transport": "s3-authenticated"}
    ranges = _probe_ranges(content_length)
    status = "pass"
    first_bytes = b""
    for (a, b) in ranges:
        t0 = time.monotonic()
        data = cfs.cat_file(path, start=a, end=b + 1)  # end is exclusive
        got = len(data)
        ok = got == (b - a + 1)
        out["reads"].append({
            "range": f"{a}-{b}", "bytes": got, "expected": b - a + 1, "ok": ok,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1)})
        if not ok:
            status = "fail"
        if a == 0:
            first_bytes = data[:64]
    out["status"] = status
    out["first_bytes_hex"] = first_bytes[:16].hex()
    out["first_bytes"] = first_bytes
    return out


# ------------------------------------------------- instrumented fsspec layer
class _CountingFile:
    def __init__(self, f, budget):
        self._f = f
        self._budget = budget

    def read(self, *a, **k):
        data = self._f.read(*a, **k)
        # each read may or may not be a request; fsspec caches blocks. Count
        # bytes conservatively; request counting happens in the FS proxy.
        self._budget.spend(len(data), 0)
        return data

    def __getattr__(self, name):
        return getattr(self._f, name)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._f.close()


class CountingFS:
    """Delegating proxy over an fsspec filesystem that counts likely-HTTP
    operations (cat_file/info/ls/open) and enforces the byte budget."""

    _COUNTED = {"cat_file", "cat", "cat_ranges", "info", "ls", "exists",
                "isdir", "isfile", "open", "get_mapper", "size"}

    def __init__(self, fs, budget: Budget):
        self._fs = fs
        self._budget = budget

    def _count_call(self, name, result=None):
        nbytes = 0
        if isinstance(result, (bytes, bytearray)):
            nbytes = len(result)
        elif isinstance(result, dict):
            nbytes = sum(len(v) for v in result.values()
                         if isinstance(v, (bytes, bytearray)))
        elif isinstance(result, list) and result and isinstance(result[0], (bytes, bytearray)):
            nbytes = sum(len(v) for v in result)
        self._budget.spend(nbytes, 1)

    def __getattr__(self, name):
        attr = getattr(self._fs, name)
        if name not in self._COUNTED or not callable(attr):
            return attr
        budget = self._budget
        count = self._count_call

        def wrapper(*a, **k):
            res = attr(*a, **k)
            if name == "open":
                budget.spend(0, 1)
                return _CountingFile(res, budget)
            count(name, res)
            return res
        return wrapper


NASA_CREDENTIALS_REQUIRED = "nasa-credentials-required"


def _counting_fs_for(url: str, budget: Budget, no_network=False,
                     credentials_url=None, earthaccess_fallback=False):
    """Return (CountingFS, path), (None, None) when no filesystem applies, or
    the sentinel (None, NASA_CREDENTIALS_REQUIRED) when the URL is NASA
    Earthdata S3 and no credentials were supplied. Credential minting is never
    attempted implicitly."""
    if fsspec is None or no_network:
        return None, None
    p = urlparse(url)
    proto = p.scheme or "file"
    if proto in ("http", "https"):
        fs = fsspec.filesystem("http")
        path = url
    elif proto == "s3":
        if credentials_url:
            fs = _AUTH_FS_CACHE.get(credentials_url)
            if fs is None:
                fs, _ = nasa_s3.get_fs(url, credentials_url=credentials_url)
                _AUTH_FS_CACHE[credentials_url] = fs
        elif earthaccess_fallback:
            fs, _ = nasa_s3.get_fs(url, earthaccess_fallback=True,
                                   credentials_url=credentials_url)
        elif nasa_s3.looks_like_nasa_earthdata(url):
            # Protected NASA bucket: never guess, never fall through to an
            # anonymous read that would 403 with a mystery reason.
            return None, NASA_CREDENTIALS_REQUIRED
        else:
            fs = fsspec.filesystem("s3", anon=True)
        path = url
    elif proto == "gs":
        fs = fsspec.filesystem("gcs", token="anon")
        path = url
    elif proto in ("", "file"):
        fs = fsspec.filesystem("file")
        path = p.path or url
    else:
        return None, None
    return CountingFS(fs, budget), path


# ----------------------------------------------------------- per-format opens
def _open_cog(url, budget, tel):
    if rasterio is None:
        tel["lazy_open"] = {"status": "skipped", "reason": "rasterio not installed"}
        return
    t0 = time.monotonic()
    req0, byt0 = budget.requests, budget.bytes
    env_opts = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
                    GDAL_HTTP_MAX_RETRY="1")
    # GDAL wheels bundle their own curl/openssl and can't be pointed at the
    # system trust store, so behind a TLS-inspecting proxy the open fails on
    # the proxy's cert even though httpx (which does the real TLS check for
    # Dimension D) verifies fine. Detect that one case and retry with GDAL
    # verification relaxed, noting it in telemetry. This never affects the
    # D3 TLS finding, which is graded from the httpx HEAD probe.
    try:
        with rasterio.Env(**env_opts):
            rasterio.open(url).close()
    except Exception as e:
        if "SSL certificate" in str(e) and "self-signed" in str(e):
            env_opts["GDAL_HTTP_UNSAFESSL"] = "YES"
            tel["notes"] = (tel.get("notes") or []) + [
                "GDAL TLS verification relaxed: TLS-inspecting proxy detected "
                "(httpx verified the real certificate for D3)"]
    with rasterio.Env(**env_opts):
        with rasterio.open(url) as src:
            open_t = time.monotonic() - t0
            # GDAL does its own HTTP; per-request counts are not visible here,
            # so estimate: header reads for a valid COG are 1-2 requests.
            tel["lazy_open"] = {
                "status": "pass", "time_to_open_s": round(open_t, 3),
                "requests_to_open": "1-2 (GDAL-managed, estimated)",
                "bytes_to_open": "~16-64 KB (estimated)",
                "driver": src.driver, "width": src.width, "height": src.height,
                "count": src.count, "dtypes": list(map(str, src.dtypes)),
                "blockshapes": [list(b) for b in src.block_shapes],
                "overviews": src.overviews(1), "crs": str(src.crs),
                "nodata": src.nodata, "compression": str(src.compression),
            }
            # subset: one block
            bh, bw = src.block_shapes[0]
            t1 = time.monotonic()
            win = rasterio.windows.Window(0, 0, min(bw, src.width), min(bh, src.height))
            arr = src.read(1, window=win)
            dt = time.monotonic() - t1
            nb = arr.nbytes
            budget.spend(min(nb, 4 * 1024 * 1024), 1)
            tel["subset_read"] = {
                "status": "pass", "what": f"block window {win}",
                "ttfb_s_approx": round(dt, 3), "bytes_decoded": nb,
                "throughput_MBps": round((nb / 1e6) / dt, 2) if dt > 0 else None}
    if cog_validate is not None:
        try:
            is_valid, errors, warnings = cog_validate(url, quiet=True)
            tel["format_validation"] = {"tool": "rio-cogeo", "valid": bool(is_valid),
                                        "errors": errors, "warnings": warnings}
        except Exception as e:
            tel["format_validation"] = {"tool": "rio-cogeo", "status": "skipped",
                                        "reason": str(e)}
    else:
        tel["format_validation"] = {"tool": "rio-cogeo", "status": "skipped",
                                    "reason": "rio-cogeo not installed"}


def _open_zarr(url, budget, tel, cfs=None, path=None, variables=None):
    if zarr is None and xr is None:
        tel["lazy_open"] = {"status": "skipped", "reason": "zarr/xarray not installed"}
        return
    if cfs is None:
        tel["lazy_open"] = {"status": "skipped", "reason": "no fsspec filesystem for scheme"}
        return
    mapper = fsspec.FSMap(path, cfs._fs)  # raw fs for compat...
    # ...but count via explicit metadata probes:
    t0 = time.monotonic()
    req0, byt0 = budget.requests, budget.bytes
    consolidated = None
    for key, ver in ((".zmetadata", 2), ("zarr.json", 3)):
        try:
            raw = cfs.cat_file(path.rstrip("/") + "/" + key)
            consolidated = {"key": key, "zarr_version": ver, "bytes": len(raw)}
            meta = json.loads(raw)
            if ver == 2:
                consolidated["n_entries"] = len(meta.get("metadata", {}))
            else:
                consolidated["has_consolidated"] = "consolidated_metadata" in meta
            break
        except Exception:
            continue
    try:
        import xarray
        ds = xarray.open_zarr(mapper, consolidated=(consolidated is not None
                                                    and consolidated.get("zarr_version") == 2))
        open_t = time.monotonic() - t0
        tel["lazy_open"] = {
            "status": "pass", "time_to_open_s": round(open_t, 3),
            "requests_to_open": budget.requests - req0,
            "bytes_to_open": budget.bytes - byt0,
            "consolidated_metadata": consolidated,
            "variables": {v: {"dims": list(ds[v].dims), "shape": list(ds[v].shape),
                              "dtype": str(ds[v].dtype),
                              "chunks": list(ds[v].encoding.get("chunks") or [])}
                          for v in list(ds.data_vars)[:20]},
            "attrs_sample": {k: str(v)[:120] for k, v in list(ds.attrs.items())[:10]},
        }
        # one-chunk decode: user-requested variable if given, else smallest
        var = None
        if variables:
            var = next((v for v in ds.data_vars if v in variables), None)
        if var is None:
            var = min(ds.data_vars, key=lambda v: ds[v].size, default=None)
        if var is not None:
            da = ds[var]
            # read one full chunk (capped ~16 MB uncompressed) — enough for
            # honest TTFB/throughput and for the compression trial grid
            cshape = list(da.encoding.get("chunks") or []) or \
                [min(64, s) for s in da.shape]
            while cshape and _np_prod(cshape) * da.dtype.itemsize > 16 * 2**20:
                m = cshape.index(max(cshape))
                cshape[m] = max(1, cshape[m] // 2)
            idx = {d: slice(0, min(c, s))
                   for d, c, s in zip(da.dims, cshape, da.shape)}
            t1 = time.monotonic()
            vals = da.isel(**idx).values
            dt = time.monotonic() - t1
            tel["subset_read"] = {"status": "pass", "what": f"one-chunk slice of {var}",
                                  "ttfb_s_approx": round(dt, 3),
                                  "bytes_decoded": int(vals.nbytes),
                                  "throughput_MBps": round((vals.nbytes / 1e6) / dt, 2)
                                  if dt > 0 else None}
            tel["compression_trials"] = _trial_codecs(vals)
        tel["format_validation"] = {"tool": "zarr", "consolidated": consolidated is not None,
                                    "one_chunk_decode": "subset_read" in tel}
    except BudgetExceeded:
        raise
    except Exception as e:
        tel["lazy_open"] = {"status": "skipped",
                            "reason": f"{type(e).__name__}: {e}",
                            "requests_before_failure": budget.requests - req0}


def _open_hdf5(url, budget, tel, cfs=None, path=None, variables=None):
    if h5py is None:
        tel["lazy_open"] = {"status": "skipped", "reason": "h5py not installed"}
        return
    if cfs is None:
        tel["lazy_open"] = {"status": "skipped", "reason": "no fsspec filesystem for scheme"}
        return
    t0 = time.monotonic()
    req0, byt0 = budget.requests, budget.bytes
    try:
        f = cfs.open(path, "rb", block_size=1024 * 1024)
        h5 = h5py.File(f, "r")
        open_t = time.monotonic() - t0
        datasets = {}

        def visit(name, obj):
            if isinstance(obj, h5py.Dataset) and len(datasets) < 25:
                cs = obj.chunks
                item = obj.dtype.itemsize
                datasets[name] = {
                    "shape": list(obj.shape), "dtype": str(obj.dtype),
                    "chunks": list(cs) if cs else None,
                    "chunk_bytes_uncompressed":
                        (int(item * __import__("math").prod(cs)) if cs else None),
                    "compression": obj.compression,
                    "layout": "chunked" if cs else "contiguous",
                }
        h5.visititems(visit)
        page = None
        try:
            fcpl = h5.id.get_create_plist()
            strategy = fcpl.get_file_space_strategy()
            page = {"strategy": str(strategy),
                    "page_size": int(fcpl.get_file_space_page_size())}
        except Exception:
            page = {"strategy": "unknown"}
        tel["lazy_open"] = {
            "status": "pass", "time_to_open_s": round(open_t, 3),
            "requests_to_open": budget.requests - req0,
            "bytes_to_open": budget.bytes - byt0,
            "datasets": datasets, "file_space": page,
        }
        # subset: read one chunk — user-requested dataset first, else first chunked
        ordered = sorted(datasets.items(),
                         key=lambda kv: 0 if variables and any(
                             v in kv[0] for v in variables) else 1)
        for name, meta in ordered:
            if meta["chunks"]:
                d = h5[name]
                sl = tuple(slice(0, c) for c in d.chunks)
                t1 = time.monotonic()
                vals = d[sl]
                dt = time.monotonic() - t1
                tel["subset_read"] = {"status": "pass", "what": f"one chunk of {name}",
                                      "ttfb_s_approx": round(dt, 3),
                                      "bytes_decoded": int(vals.nbytes),
                                      "throughput_MBps":
                                      round((vals.nbytes / 1e6) / dt, 2) if dt > 0 else None}
                tel["compression_trials"] = _trial_codecs(vals)
                break
        tel["format_validation"] = {"tool": "h5py-introspection",
                                    "note": "h5stat-style chunk/page inspection",
                                    "file_space": page}
        h5.close()
    except BudgetExceeded:
        raise
    except Exception as e:
        tel["lazy_open"] = {"status": "skipped", "reason": f"{type(e).__name__}: {e}",
                            "requests_before_failure": budget.requests - req0}


def _open_parquet(url, budget, tel, cfs=None, path=None, variables=None):
    if papq is None:
        tel["lazy_open"] = {"status": "skipped", "reason": "pyarrow not installed"}
        return
    if cfs is None:
        tel["lazy_open"] = {"status": "skipped", "reason": "no fsspec filesystem for scheme"}
        return
    t0 = time.monotonic()
    req0, byt0 = budget.requests, budget.bytes
    try:
        f = cfs.open(path, "rb")
        pf = papq.ParquetFile(f)
        open_t = time.monotonic() - t0
        md = pf.metadata
        rg_sizes = [md.row_group(i).total_byte_size for i in range(min(md.num_row_groups, 200))]
        geo = None
        kv = md.metadata or {}
        for k, v in kv.items():
            if k == b"geo":
                try:
                    geo = json.loads(v.decode())
                except Exception:
                    geo = {"raw": v[:200].decode(errors="replace")}
        tel["lazy_open"] = {
            "status": "pass", "time_to_open_s": round(open_t, 3),
            "requests_to_open": budget.requests - req0,
            "bytes_to_open": budget.bytes - byt0,
            "num_rows": md.num_rows, "num_row_groups": md.num_row_groups,
            "num_columns": md.num_columns,
            "compression": (md.row_group(0).column(0).compression
                            if md.num_row_groups else None),
            "row_group_bytes": {"min": min(rg_sizes) if rg_sizes else None,
                                "median": sorted(rg_sizes)[len(rg_sizes) // 2] if rg_sizes else None,
                                "max": max(rg_sizes) if rg_sizes else None},
            "geo_metadata": geo,
        }
        t1 = time.monotonic()
        col = pf.schema_arrow.names[0]
        if variables:
            col = next((c for c in pf.schema_arrow.names if c in variables), col)
        tbl = pf.read_row_group(0, columns=[col])
        try:
            _np_col = tbl.column(0).combine_chunks().to_numpy(zero_copy_only=False)
            tel["compression_trials"] = _trial_codecs(_np_col)
        except Exception:
            pass
        dt = time.monotonic() - t1
        nb = tbl.nbytes
        budget.spend(min(nb, 4 * 1024 * 1024), 1)
        tel["subset_read"] = {"status": "pass", "what": f"row group 0, column {col!r}",
                              "ttfb_s_approx": round(dt, 3), "bytes_decoded": int(nb),
                              "throughput_MBps": round((nb / 1e6) / dt, 2) if dt > 0 else None}
        tel["format_validation"] = {"tool": "pyarrow", "footer_parsed": True,
                                    "row_group_stats_present":
                                        md.row_group(0).column(0).statistics is not None
                                        if md.num_row_groups else None}
    except BudgetExceeded:
        raise
    except Exception as e:
        tel["lazy_open"] = {"status": "skipped", "reason": f"{type(e).__name__}: {e}",
                            "requests_before_failure": budget.requests - req0}


_OPENERS = {
    "cog": _open_cog, "geotiff": _open_cog,
    "zarr": _open_zarr, "icechunk": _open_zarr, "kerchunk": _open_zarr,
    "hdf5": _open_hdf5, "netcdf4": _open_hdf5,
    "parquet": _open_parquet, "geoparquet": _open_parquet,
}


# ------------------------------------------------------------------ main entry
def _https_probe_with_bearer(probe_url, budget, tel):
    """HEAD (+ one EDL-bearer retry for protected HTTPS inputs). Returns the
    Authorization headers to reuse for the ranged probe, or None."""
    tel["head"] = head_probe(probe_url, budget)
    h = tel["head"]
    edl_redirect = nasa_s3.EDL_HOST in str(h.get("final_url") or "")
    if not (h.get("http_status") in (401, 403) or edl_redirect):
        return None
    token = nasa_s3.edl_bearer_token()
    if not token:
        h["bearer_retry"] = ("skipped: no Earthdata Login token available "
                             "(set EARTHDATA_TOKEN, EARTHDATA_USERNAME/"
                             "EARTHDATA_PASSWORD, or ~/.netrc)")
        return None
    headers = {"Authorization": f"Bearer {token}"}
    retry = head_probe(probe_url, budget, headers=headers)
    retry["bearer_auth"] = True
    tel["head"] = retry
    return headers if retry.get("status") == "pass" else None


def _ranged_auth_recovery(probe_url, budget, ranged, content_length, headers):
    """Some protected HTTPS endpoints answer HEAD but 401 the ranged GET.
    Retry once with an EDL bearer token; if it still fails, the reads are
    auth-blocked — SKIPPED, never FAILED."""
    def auth_blocked(r):
        return bool({rd.get("http_status") for rd in r.get("reads") or []}
                    & {401, 403})

    if not auth_blocked(ranged):
        return ranged
    if headers is None:
        token = nasa_s3.edl_bearer_token()
        if token:
            retry = ranged_probe(probe_url, budget, content_length,
                                 headers={"Authorization": f"Bearer {token}"})
            retry["bearer_auth"] = True
            ranged = retry
            if not auth_blocked(ranged):
                return ranged
        else:
            ranged["bearer_retry"] = ("skipped: no Earthdata Login token "
                                      "available (set EARTHDATA_TOKEN, "
                                      "EARTHDATA_USERNAME/EARTHDATA_PASSWORD, "
                                      "or ~/.netrc)")
    ranged["status"] = "skipped"
    ranged["auth_blocked"] = True
    ranged["reason"] = CREDENTIALS_AUTH_REASON
    return ranged


def run_smoke_test(url: str, fmt: str = "", no_network: bool = False,
                   variables=None, credentials_url=None,
                   earthaccess_fallback: bool = False) -> dict:
    """Run the full bounded smoke test for one asset. Never raises for
    network problems; returns status pass|fail|skipped with telemetry."""
    budget = Budget()
    tel = {"url": url, "format": fmt, "caps": {"bytes": BYTE_CAP, "seconds": TIME_CAP}}
    scheme = urlparse(url).scheme
    # A credentialed NASA S3 read is a first-class transport: never rewritten
    # to a public HTTPS URL (that HEAD would 403 and mask the real result).
    credentialed_s3 = scheme == "s3" and bool(credentials_url or earthaccess_fallback)

    if no_network:
        tel.update(status="skipped", reason="network disabled (--no-network)",
                   budget=budget.snapshot())
        return tel

    if (scheme == "s3" and not credentialed_s3
            and nasa_s3.looks_like_nasa_earthdata(url)):
        tel.update(status="skipped", reason=NASA_CREDENTIALS_REQUIRED_REASON,
                   budget=budget.snapshot())
        return tel

    probe_url = url if credentialed_s3 else (
        _to_https(url) if scheme in ("s3", "gs") else url)
    is_remote = not credentialed_s3 and urlparse(probe_url).scheme in ("http", "https")

    cfs = path = None
    try:
        if credentialed_s3:
            try:
                cfs, path = _counting_fs_for(
                    url, budget, no_network, credentials_url=credentials_url,
                    earthaccess_fallback=earthaccess_fallback)
                if cfs is None:
                    tel.update(status="skipped",
                               reason="fsspec not installed; cannot read "
                                      "authenticated NASA S3",
                               budget=budget.snapshot())
                    return tel
                if fmt not in ("zarr", "icechunk"):  # store roots aren't objects
                    tel["head"] = fs_head_probe(cfs, path, budget)
                    tel["ranged"] = fs_ranged_probe(
                        cfs, path, budget, tel["head"].get("content_length"))
                    tel["ranged"].pop("first_bytes", None)
            except BudgetExceeded:
                raise
            except Exception as e:
                reason = _s3_auth_skip_reason(e)
                tel.update(
                    status="skipped",
                    reason=reason or f"s3-authenticated: {type(e).__name__}: {e}",
                    budget=budget.snapshot())
                return tel
        elif is_remote:
            headers = None
            if scheme in ("http", "https"):
                # Bearer auth is for HTTPS inputs only — never a stand-in for
                # credentials on an s3:// input.
                headers = _https_probe_with_bearer(probe_url, budget, tel)
            else:
                tel["head"] = head_probe(probe_url, budget)
            if tel["head"].get("status") == "skipped":
                tel.update(status="skipped", reason=tel["head"].get("reason"),
                           budget=budget.snapshot())
                return tel
            if fmt not in ("zarr", "icechunk"):  # store roots aren't single objects
                content_length = tel["head"].get("content_length")
                tel["ranged"] = ranged_probe(probe_url, budget, content_length,
                                             headers=headers)
                if scheme in ("http", "https"):
                    tel["ranged"] = _ranged_auth_recovery(
                        probe_url, budget, tel["ranged"], content_length, headers)
                tel["ranged"].pop("first_bytes", None)
                if tel["ranged"].get("auth_blocked"):
                    tel.update(status="skipped", reason=tel["ranged"]["reason"],
                               budget=budget.snapshot())
                    return tel
        opener = _OPENERS.get(fmt)
        if opener is not None:
            if opener is _open_cog:
                if credentialed_s3:
                    tel["lazy_open"] = {
                        "status": "skipped",
                        "reason": "GDAL cannot use the minted NASA credential "
                                  "provider; re-run in-region with AWS "
                                  "credentials exported for a COG open"}
                else:
                    opener(url if scheme not in ("s3", "gs") else probe_url,
                           budget, tel)
            else:
                if cfs is None:
                    cfs, path = _counting_fs_for(
                        url, budget, no_network, credentials_url=credentials_url,
                        earthaccess_fallback=earthaccess_fallback)
                    if path == NASA_CREDENTIALS_REQUIRED:  # pragma: no cover
                        tel.update(status="skipped",
                                   reason=NASA_CREDENTIALS_REQUIRED_REASON,
                                   budget=budget.snapshot())
                        return tel
                opener(url, budget, tel, cfs, path, variables=variables)
        else:
            tel["lazy_open"] = {"status": "n/a", "open_supported": False,
                                "reason": f"no remote-open path for format {fmt!r} "
                                          "(legacy formats: hosting quality only)"}
        # verdict
        hard_fail = (
            tel.get("head", {}).get("status") == "fail"
            or tel.get("ranged", {}).get("status") == "fail"
            or tel.get("format_validation", {}).get("valid") is False
        )
        lazy = tel.get("lazy_open", {}).get("status")
        lazy_reason = str(tel.get("lazy_open", {}).get("reason", ""))
        # Auth/region failures surfaced by the opener (e.g. a Zarr store root,
        # where there is no single object to HEAD first) are SKIPPED, not FAIL.
        auth_reason = (_s3_auth_skip_reason(Exception(lazy_reason))
                       if credentialed_s3 and lazy == "skipped" else None)
        if auth_reason:
            tel["status"] = "skipped"
            tel["reason"] = auth_reason
        elif hard_fail:
            tel["status"] = "fail"
        elif lazy == "skipped" and "network" in lazy_reason:
            tel["status"] = "skipped"
            tel["reason"] = tel["lazy_open"]["reason"]
        else:
            tel["status"] = "pass"
    except BudgetExceeded as e:
        tel["status"] = "fail"
        tel["reason"] = f"budget exceeded: {e} (dataset forces oversized reads)"
    except Exception as e:  # pragma: no cover - safety net
        tel["status"] = "skipped"
        tel["reason"] = f"unexpected: {type(e).__name__}: {e}"
    tel["budget"] = budget.snapshot()
    return tel


def resolve_credentials_url(credentials_url=None, granule_id=None):
    """--credentials-url wins; otherwise resolve a CMR granule ID to its
    DAAC s3credentials endpoint. Never raises: an unresolvable granule leaves
    the credentials endpoint unset (the asset is then SKIPPED, not FAILED)."""
    if credentials_url:
        return credentials_url
    if not granule_id:
        return None
    try:
        resolved = resolve_granule.resolve(granule_id).get("credentials_url")
    except SystemExit as e:  # resolve() is CLI-style: sys.exit(msg)
        print(f"[smoke_test] granule resolution failed: {e}", file=sys.stderr)
        return None
    if not resolved:
        print(f"[smoke_test] granule {granule_id} has no s3credentials "
              "endpoint in CMR", file=sys.stderr)
    return resolved


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--format", default="", help="cog|zarr|hdf5|parquet|...")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--credentials-url",
                    help="DAAC s3credentials endpoint for NASA Earthdata S3 "
                         "(wins over --granule-id)")
    ap.add_argument("--granule-id",
                    help="CMR granule concept ID (e.g. G4289749526-ASF); "
                         "resolved to a credentials endpoint via CMR")
    ap.add_argument("--earthaccess-fallback", action="store_true",
                    help="opt-in earthaccess credential fallback (never "
                         "used implicitly)")
    args = ap.parse_args()
    credentials_url = resolve_credentials_url(args.credentials_url,
                                              args.granule_id)
    print(json.dumps(run_smoke_test(args.url, args.format, args.no_network,
                                    credentials_url=credentials_url,
                                    earthaccess_fallback=args.earthaccess_fallback),
                     indent=2, default=str))


if __name__ == "__main__":
    main()
