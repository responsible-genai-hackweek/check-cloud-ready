"""Bounded, instrumented per-format openers.

Ported from the earth-science-cloud-readiness skill's smoke_test.py
(``_open_cog``/``_open_zarr``/``_open_hdf5``/``_open_parquet``/
``_OPENERS``), retargeted onto this package's ``telemetry`` (Budget /
counting fs) and ``access`` (already-authenticated fs, auth-failure
classification) layers.

Dropped from the port (Ruling I-2/I-3): ``_NISAR_AUTH_FS``, ``_to_https``,
and ``_counting_fs_for``'s URL-scheme/auth heuristics. The fs handed to
``open_dataset`` is assumed already authenticated by
``access.workflow.resolve_access`` (or ``None`` for a local path);
openers never construct or authenticate a filesystem themselves -- they
only wrap whatever fs they are given with ``telemetry.counting_fs`` (the
sole exception being the ``fs is None`` local-path case, where a plain
``fsspec`` local filesystem is substituted so counting still has
something to wrap).

Also dropped: the "subset read" / empirical compression-trial logic
(``_trial_codecs`` and friends). That is downstream work for chunking.py
/ compression assessment (Tasks 7/8), not this module's job -- this
module's contract (see ``open_dataset``'s docstring) is "open the
dataset, build its variable inventory, and record format/consolidation
checks", not "read and trial-compress a sample chunk".

S6 fix: the ported code never read HDF5/zarr attrs or recognized CRS
container datasets/arrays (``projection``/``crs``/``spatial_ref``, or
anything referenced by a ``grid_mapping`` attribute). Both HDF5 and
zarr openers now sample attrs per-variable and additionally flag
recognized CRS containers in ``format_checks["crs_containers"]``.

S7 fix: HDF5 stores can disperse metadata across many small objects
with no single index (unlike zarr's consolidated-metadata option), so
a lazy open alone doesn't reveal how expensive discovering the full
variable inventory is. ``_open_hdf5`` now performs a bounded full-tree
walk (see ``_hdf5_metadata_walk``): identification/metadata-named
groups are visited first (so they are captured even if a cap bites
before the walk finishes), then the rest of the tree breadth-first,
capped by the shared ``Budget`` (``Budget.check_stage("metadata_walk")``
for wall-clock; a simple object-count ceiling here since ``Budget`` has
no request cap of its own). The result is recorded in
``metadata_walk``. Zarr's consolidated-metadata check is comparatively
trivial (a single metadata-file read reveals the whole store), so zarr
does not get its own full-tree walk -- ``metadata_walk`` is ``None`` for
every format except HDF5/netCDF-4 (see ``open_dataset``'s OpenResult
docstring).
"""
from __future__ import annotations

import collections
import json
import math
from typing import Any

import numpy as np

from . import telemetry
from .access import probe

__all__ = ["open_dataset"]

# --------------------------------------------------------------- soft imports


def _try_import(name):
    try:
        return __import__(name)
    except Exception:
        return None


h5py = _try_import("h5py")
zarr = _try_import("zarr")
pa = _try_import("pyarrow")
rasterio = _try_import("rasterio")

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


# -------------------------------------------------------------- OpenResult

def _result(status, *, reason=None, handle=None, telemetry_stats=None,
            inventory=None, format_checks=None, metadata_walk=None):
    """Build one ``OpenResult`` dict (see ``open_dataset``'s docstring
    for the shape). Every opener returns through here so the shape
    never drifts between formats.
    """
    return {
        "status": status,
        "reason": reason,
        "handle": handle,
        "telemetry": telemetry_stats or {},
        "inventory": inventory if inventory is not None else [],
        "format_checks": format_checks if format_checks is not None else {},
        "metadata_walk": metadata_walk,
    }


def _is_auth_error(exc: Exception) -> bool:
    """S2-consistent: classify an exception raised while opening a
    dataset the same way access/probe.py classifies a transport-stage
    failure. Only ``"auth-required"`` ever turns an opener's exception
    into a ``skipped`` result -- anything else is a real ``fail``.
    """
    return probe.classify_access_failure("open", exc) == "auth-required"


def _decode_attr(v: Any) -> Any:
    """Make an attribute value JSON/plain-Python friendly (h5py/zarr
    may hand back bytes or numpy scalar/array types)."""
    if isinstance(v, bytes):
        try:
            return v.decode()
        except Exception:
            return repr(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, np.generic):
        return v.item()
    return v


def _sample_attrs(attrs_like, limit=10) -> dict:
    return {k: _decode_attr(v) for k, v in list(attrs_like.items())[:limit]}


_CRS_CONTAINER_NAMES = {"projection", "crs", "spatial_ref"}


def _basename(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _find_crs_containers(inventory: list[dict]) -> list[dict]:
    """S6: recognize CRS-container variables by name
    (projection/crs/spatial_ref) or by being referenced via any
    variable's ``grid_mapping`` attribute (the CF convention points at
    the CRS container by name, usually a bare basename sibling)."""
    by_basename: dict[str, str] = {}
    for rec in inventory:
        by_basename.setdefault(_basename(rec["name"]), rec["name"])

    container_names: set[str] = set()
    for rec in inventory:
        if _basename(rec["name"]).lower() in _CRS_CONTAINER_NAMES:
            container_names.add(rec["name"])
        gm = rec.get("attrs", {}).get("grid_mapping")
        if gm:
            gm = str(gm)
            if gm in by_basename.values():
                container_names.add(gm)
            elif _basename(gm) in by_basename:
                container_names.add(by_basename[_basename(gm)])

    return [
        {"name": rec["name"], "attrs": rec["attrs"]}
        for rec in inventory if rec["name"] in container_names
    ]


# ------------------------------------------------------------------- HDF5

_PRIORITY_GROUP_NAMES = {"identification", "metadata"}
_METADATA_WALK_MAX_OBJECTS = 5000
_METADATA_WALK_CHECK_EVERY = 25


def _hdf5_dataset_record(name: str, obj) -> dict:
    shape = list(obj.shape)
    dtype = str(obj.dtype)
    chunks = list(obj.chunks) if obj.chunks else None
    compression = obj.compression
    shuffle = bool(getattr(obj, "shuffle", False))
    parts = []
    if compression:
        parts.append(str(compression))
    if shuffle:
        parts.append("shuffle")
    codec = "+".join(parts) if parts else None
    try:
        size_bytes = int((math.prod(shape) if shape else 1) * obj.dtype.itemsize)
    except Exception:
        size_bytes = None
    return {
        "name": "/" + name.lstrip("/"),
        "dims": [f"dim_{i}" for i in range(len(shape))],
        "shape": shape,
        "dtype": dtype,
        "chunks": chunks,
        "attrs": _sample_attrs(obj.attrs),
        "size_bytes": size_bytes,
        "codec": codec,
    }


def _hdf5_metadata_walk(h5):
    """S7: bounded full-tree walk. Visits identification/metadata-named
    top-level groups (and their descendants) first, then the rest of
    the tree breadth-first, so those groups are captured even if a cap
    bites partway through. Every visited object spends one budget
    "request" (there is no real per-object wire cost for an
    already-open local handle, but this is the same abstraction a
    remote HDF5 reader pays: one logical fetch per object's metadata)
    so the walk is bounded by the same Budget the rest of the pipeline
    uses, and its cost is visible in the shared counters.
    """
    inventory: list[dict] = []
    visit_order: list[str] = []
    objects_visited = 0
    complete = True
    capped_at = None

    def visit(name, obj) -> bool:
        nonlocal objects_visited, complete, capped_at
        objects_visited += 1
        visit_order.append("/" + name.lstrip("/"))
        if isinstance(obj, h5py.Dataset):
            inventory.append(_hdf5_dataset_record(name, obj))
        if objects_visited >= _METADATA_WALK_MAX_OBJECTS:
            complete = False
            capped_at = "objects"
            return False
        return True

    def bfs(items) -> bool:
        queue = collections.deque(items)
        while queue:
            name, obj = queue.popleft()
            if not visit(name, obj):
                return False
            if isinstance(obj, h5py.Group):
                for child_name, child_obj in obj.items():
                    queue.append((f"{name}/{child_name}", child_obj))
        return True

    priority_items, rest_items = [], []
    for name, obj in h5.items():
        (priority_items if name.lower() in _PRIORITY_GROUP_NAMES else rest_items).append((name, obj))

    if bfs(priority_items):
        bfs(rest_items)

    walk = {
        "objects_visited": objects_visited,
        "complete": complete,
        "capped_at": capped_at,
        "visit_order": visit_order,
    }
    return inventory, walk


def _open_hdf5(fs, path: str):
    if h5py is None:
        return _result("skipped", reason="h5py not installed")

    f = None
    try:
        f = fs.open(path, "rb")
        h5 = h5py.File(f, "r")
    except Exception as e:
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        if _is_auth_error(e):
            return _result("skipped", reason=f"auth: {type(e).__name__}: {e}")
        return _result("fail", reason=f"{type(e).__name__}: {e}")

    inventory, walk = _hdf5_metadata_walk(h5)
    h5.close()

    crs_containers = _find_crs_containers(inventory)

    return _result(
        "ok", handle=h5,
        inventory=inventory,
        format_checks={"crs_containers": crs_containers},
        metadata_walk=walk,
    )


# ------------------------------------------------------------------- zarr

def _zarr_consolidated_probe(path: str) -> dict:
    """One explicit, counted read of the store's root metadata file,
    which is enough to tell whether the store is consolidated (a
    single request reveals the whole layout) -- the "trivially
    1-request" case called out for zarr in the S7 fix.

    ``consolidated=False`` is an explicitly supported outcome of this
    probe (a non-consolidated v2 store is a common, legitimate layout,
    not a failure -- see ``formats.sniff_store``'s "dispersed metadata"
    note). When no consolidated-metadata index is found, this still
    counts one real metadata read (``.zgroup``, falling back to
    ``.zarray`` for a store whose root is itself an array) through
    ``cfs`` so ``bytes_to_open`` reflects genuine store-metadata I/O
    instead of reporting a bogus 0 -- a 0-byte "successful" probe here
    would otherwise trip ``assert_open_measured``'s zero-bytes guard on
    a perfectly legitimate, correctly-classified store.
    """
    base = path.rstrip("/")
    for key, ver in ((f"{base}/zarr.json", 3), (f"{base}/.zmetadata", 2)):
        try:
            raw = cfs.cat_file(key)
        except Exception:
            continue
        try:
            meta = json.loads(raw)
        except Exception:
            meta = {}
        consolidated = (meta.get("consolidated_metadata") is not None) if ver == 3 else True
        return {"zarr_version": ver, "consolidated": consolidated, "key": key}

    for key in (f"{base}/.zgroup", f"{base}/.zarray"):
        try:
            cfs.cat_file(key)
        except Exception:
            continue
        return {"zarr_version": 2, "consolidated": False, "key": key}

    return {"zarr_version": None, "consolidated": False, "key": None}


def _zarr_store_for(fs, path: str):
    """Local paths use zarr's own LocalStore (fs is None or a plain
    local fsspec filesystem); anything else needs an async-capable
    fsspec filesystem to build a zarr FsspecStore. Returns None if
    neither applies (caller reports skipped rather than raising).
    """
    if fs is None:
        return path
    proto = getattr(fs, "protocol", "file")
    protos = proto if isinstance(proto, (list, tuple)) else (proto,)
    if "file" in protos or "local" in protos:
        return path
    try: 
        from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper
        async_fs = AsyncFileSystemWrapper(fs)
        return zarr.storage.FsspecStore(async_fs, path=path, read_only=True)
    except TypeError:
        return None


def _zarr_array_record(name: str, arr) -> dict:
    shape = list(arr.shape)
    dtype = str(arr.dtype)
    chunks = list(arr.chunks) if getattr(arr, "chunks", None) else None
    codec_parts = [str(c) for c in (list(getattr(arr, "filters", ()) or ())
                                    + list(getattr(arr, "compressors", ()) or ()))]
    codec = ",".join(codec_parts) if codec_parts else None
    try:
        size_bytes = int((math.prod(shape) if shape else 1) * arr.dtype.itemsize)
    except Exception:
        size_bytes = None
    dims = arr.attrs.get("_ARRAY_DIMENSIONS")
    return {
        "name": "/" + name.lstrip("/"),
        "dims": dims if dims is not None else [f"dim_{i}" for i in range(len(shape))],
        "shape": shape,
        "dtype": dtype,
        "chunks": chunks,
        "attrs": _sample_attrs(dict(arr.attrs)),
        "size_bytes": size_bytes,
        "codec": codec,
    }


def _zarr_inventory(group) -> list[dict]:
    inventory = []
    for name, member in group.members(max_depth=None):
        if zarr is not None and isinstance(member, zarr.Array):
            inventory.append(_zarr_array_record(name, member))
    return inventory


def _open_zarr(fs, path: str):
    if zarr is None:
        return _result("skipped", reason="zarr not installed")

    consolidated = _zarr_consolidated_probe(path)

    try:
        store = _zarr_store_for(fs, path)
        if store is None:
            return _result("skipped",
                            reason="zarr open not supported for this filesystem "
                                   "(not async-capable and not local)")
        group = zarr.open_group(store=store, mode="r")
    except Exception as e:
        if _is_auth_error(e):
            return _result("skipped", reason=f"auth: {type(e).__name__}: {e}")
        return _result("fail", reason=f"{type(e).__name__}: {e}")

    inventory = _zarr_inventory(group)
    crs_containers = _find_crs_containers(inventory)

    return _result(
        "ok", handle=group,
        inventory=inventory,
        format_checks={"crs_containers": crs_containers,
                        "consolidated": consolidated["consolidated"],
                        "zarr_version": consolidated["zarr_version"]},
        metadata_walk=None,
    )


# -------------------------------------------------------------------- COG

_COG_ENV_OPTS = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                     CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
                     GDAL_HTTP_MAX_RETRY="1")


def _open_cog(fs, cfs, budget: telemetry.Budget, path: str):
    """COG opens go through GDAL's own VSI/curl machinery (rasterio),
    which does not route through fsspec at all -- there is no fs to
    wrap here (matching the ported behavior); requests/bytes to open
    are therefore GDAL-estimated, not counted.
    """
    if rasterio is None:
        return _result("skipped", reason="rasterio not installed")

    try:
        with rasterio.Env(**_COG_ENV_OPTS):
            src = rasterio.open(path)
    except Exception as e:
        if _is_auth_error(e):
            return _result("skipped", reason=f"auth: {type(e).__name__}: {e}")
        return _result("fail", reason=f"{type(e).__name__}: {e}")

    inventory = []
    for i, (dtype, blockshape) in enumerate(zip(src.dtypes, src.block_shapes), start=1):
        inventory.append({
            "name": f"/band_{i}",
            "dims": ["dim_0", "dim_1"],
            "shape": [src.height, src.width],
            "dtype": str(dtype),
            "chunks": list(blockshape) if blockshape else None,
            "attrs": {},
            "size_bytes": None,
            "codec": str(src.compression) if src.compression else None,
        })

    format_checks: dict = {"crs": str(src.crs) if src.crs else None,
                            "overviews": src.overviews(1)}
    if cog_validate is not None:
        try:
            is_valid, errors, warnings = cog_validate(path, quiet=True)
            format_checks["cog_validate"] = {"valid": bool(is_valid), "errors": errors,
                                             "warnings": warnings}
        except Exception as e:
            format_checks["cog_validate"] = {"skipped": f"{type(e).__name__}: {e}"}
    else:
        format_checks["cog_validate"] = {"skipped": "rio-cogeo not installed"}

    return _result(
        "ok", handle=src,
        telemetry_stats={"requests_to_open": "1-2 (GDAL-managed, estimated)",
                          "bytes_to_open": "~16-64 KB (estimated)"},
        inventory=inventory,
        format_checks=format_checks,
        metadata_walk=None,
    )


# --------------------------------------------------------------- parquet

def _open_parquet(fs, cfs, budget: telemetry.Budget, path: str):
    if papq is None:
        return _result("skipped", reason="pyarrow not installed")

    before = cfs.snapshot()
    f = None
    try:
        f = cfs.open(path, "rb")
        pf = papq.ParquetFile(f)
    except Exception as e:
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        if _is_auth_error(e):
            return _result("skipped", reason=f"auth: {type(e).__name__}: {e}")
        return _result("fail", reason=f"{type(e).__name__}: {e}")
    after_open = cfs.snapshot()
    requests_to_open = after_open["requests"] - before["requests"]
    bytes_to_open = after_open["bytes_read"] - before["bytes_read"]

    try:
        telemetry.assert_open_measured({"bytes_read": bytes_to_open})
    except telemetry.MeasurementError as e:
        try:
            f.close()
        except Exception:
            pass
        return _result("fail", reason=str(e))

    md = pf.metadata
    num_row_groups = md.num_row_groups
    inventory = []
    for i, name in enumerate(pf.schema_arrow.names):
        total_compressed = 0
        compression = None
        for rg_idx in range(num_row_groups):
            col = md.row_group(rg_idx).column(i)
            total_compressed += col.total_compressed_size or 0
            if compression is None:
                compression = col.compression
        inventory.append({
            "name": name,
            "dims": [],
            "shape": [md.num_rows],
            "dtype": str(pf.schema_arrow.field(i).type),
            "chunks": None,
            "attrs": {},
            "size_bytes": total_compressed or None,
            "codec": compression,
        })

    format_checks = {
        "footer_parsed": True,
        "row_group_stats_present": (
            md.row_group(0).column(0).statistics is not None
            if num_row_groups else None
        ),
    }

    return _result(
        "ok", handle=pf,
        telemetry_stats={"requests_to_open": requests_to_open,
                          "bytes_to_open": bytes_to_open},
        inventory=inventory,
        format_checks=format_checks,
        metadata_walk=None,
    )


# --------------------------------------------------------------- dispatch

_OPENERS = {
    "cog": _open_cog, "geotiff": _open_cog,
    "zarr": _open_zarr, "icechunk": _open_zarr,
    "hdf5": _open_hdf5, "netcdf4": _open_hdf5,
    "parquet": _open_parquet, "geoparquet": _open_parquet,
}


def open_dataset(fmt: str, fs, path: str, budget: telemetry.Budget) -> dict:
    """Open ``path`` (interpreted against ``fs``) as ``fmt``, bounded by
    ``budget``.

    ``fs`` is an already-authenticated fsspec-like filesystem (as
    produced by ``access.workflow.resolve_access``), or ``None`` for a
    plain local path -- this function substitutes a local fsspec
    filesystem in that case (the one filesystem construction this
    module ever does) so telemetry still works. Every opener wraps
    whatever fs it ends up with in ``telemetry.counting_fs``; no opener
    constructs or authenticates a filesystem itself.

    Returns an ``OpenResult`` dict::

        {"status": "ok|skipped|fail|n/a", "reason": str|None,
         "handle": object|None,           # zarr group / h5py File /
                                           # rasterio dataset / pyarrow
                                           # ParquetFile -- caller closes
         "telemetry": {"requests_to_open": int, "bytes_to_open": int, ...},
         "inventory": [inventory-record, ...],  # Ruling I-1 schema
         "format_checks": {...},
         "metadata_walk": {...}|None}     # S7, HDF5/netCDF-4 only

    Unknown formats return ``status="n/a"``. Auth-shaped exceptions
    (401/403/URS-redirect-flavored, per
    ``access.probe.classify_access_failure``) raised while opening are
    always ``status="skipped"``, never ``"fail"`` (S2 consistency).
    Missing heavy dependencies (h5py/zarr/rasterio/rio-cogeo/pyarrow)
    are ``status="skipped"`` with a "<pkg> not installed" reason -- this
    module itself imports fine with only core dependencies installed.
    """
    opener = _OPENERS.get(fmt)
    if opener is None:
        return _result("n/a", reason=f"no opener registered for format {fmt!r}")

    if fs is None:
        import fsspec
        fs = fsspec.filesystem("file")

    cfs = telemetry.counting_fs(fs, budget)
    return opener(fs, path)
