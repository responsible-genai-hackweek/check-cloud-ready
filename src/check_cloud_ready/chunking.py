"""Chunk measurement, grading, S4 interior sampling, and orientation
declaration.

Ported from two sources (see the task brief for line references):

- ``.claude/skills/check-cloud-ready/scripts/chunk_report.py``: the
  ``grade()`` band edges, the stored-chunk-size sampling idea
  (``sample_zarr_chunk_sizes`` / HDF5 ``get_num_chunks``/
  ``get_chunk_info``), ``describe_array``'s uncompressed-size math, and
  the contiguous-HDF5-layout note.
- ``.claude/skills/earth-science-cloud-readiness/scripts/assess.py``:
  ``_estimate_chunk_mb``/``_estimate_chunk_count`` as *fallback
  estimators*, used only when measurement is impossible (no zarr/h5py,
  no stored chunks found, sampling error). Measured values always win;
  every ``compressed_chunk_mb``-shaped result is explicitly labeled
  ``measured`` or ``estimated`` -- never silently blended.

S4 upgrade over the ported chunk_report.py: the ported script samples
the *first N stored objects* regardless of content, which can silently
report the compressed size of all-fill/all-NaN chunks (common at
tile/grid corners) as if it were representative data. This module
instead samples chunks *near the array centroid, spiraling outward*
(``pick_interior_chunks``) and rejects a candidate if >=90% of its
decoded values are fill/NaN (``is_data_bearing``) before trusting its
size as compression/throughput telemetry. Both functions are
dependency-light (numpy only) and exported for reuse by
``compression.py`` (Task 8).

``declare_orientation`` is the plain-language "what is this chunking
optimized for" feature: given dims/shape/chunks it classifies the
dataset as map-optimized / timeseries-optimized / balanced / tiled
(no time-like dim) / unknown, and computes the two canonical queries
(full spatial extent at one time; full-depth series at one point) with
a chunks-touched count and a bytes-amplification estimate. Amplification
is only *graded* (warn >10x, fail >100x) when a ``use_case`` is given;
without one it is still reported as evidence with ``grade: None``.
"""
from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np

__all__ = [
    "assess_chunking",
    "grade",
    "sample_chunk_sizes",
    "pick_interior_chunks",
    "is_data_bearing",
    "declare_orientation",
]

# ------------------------------------------------------------------- grade

# Ported band edges from chunk_report.py's grade(), verbatim:
#   <1 MB fail / 1-4 MB warn / 4-16 MB pass / 16-64 MB pass (lean note) / >64 MB warn
_FAIL_MAX = 1.0
_WARN_LOW_MAX = 4.0
_PASS_MAX = 16.0
_LEAN_MAX = 64.0


def grade(compressed_mb: float | None) -> tuple[str, str]:
    """Grade a compressed chunk size in MB against the ~8-16 MB
    provider-agnostic wire target (acceptable 4-64 MB, floor ~1 MB;
    see chunking-for-ai.md). Returns ``(band, note)``.
    """
    if compressed_mb is None:
        return "unknown", "no measured or estimated compressed size available"
    if compressed_mb < _FAIL_MAX:
        return "fail", f"{compressed_mb:.3g} MB < 1 MB - too small, per-request overhead dominates"
    if compressed_mb < _WARN_LOW_MAX:
        return "warn", (f"{compressed_mb:.3g} MB in 1-4 MB - below the ~8-16 MB wire-target "
                         "sweet spot")
    if compressed_mb <= _PASS_MAX:
        return "pass", f"{compressed_mb:.3g} MB in 4-16 MB - within the wire-target sweet spot"
    if compressed_mb <= _LEAN_MAX:
        return "pass", (f"{compressed_mb:.3g} MB in 16-64 MB - fine, but lean toward the "
                         "smaller end if reads are latency-sensitive (partial-read waste "
                         "on large chunks)")
    return "warn", f"{compressed_mb:.3g} MB > 64 MB - large chunks tax partial reads and retries"


# ---------------------------------------------------- fallback estimators (S4)
# Ported from assess.py's _estimate_chunk_mb/_estimate_chunk_count, adapted to
# operate directly on (shape, chunks, dtype) instead of the skill's ad-hoc
# "lo" report dict. Used ONLY when measurement is impossible.

_ASSUMED_RATIO = 2.0  # ported "assume 2:1" compression ratio


def _itemsize(dtype: Any) -> int:
    try:
        return np.dtype(dtype).itemsize
    except TypeError:
        return 8


def _estimate_compressed_mb(chunks, dtype, ratio: float = _ASSUMED_RATIO) -> float | None:
    if not chunks:
        return None
    n_elem = math.prod(chunks)
    return round(n_elem * _itemsize(dtype) / ratio / 2**20, 3)


def _estimate_chunk_count(shape, chunks) -> int | None:
    if not shape or not chunks:
        return None
    try:
        return math.prod(math.ceil(s / c) for s, c in zip(shape, chunks))
    except ZeroDivisionError:
        return None


# --------------------------------------------------------- S4 interior sampling
# numpy-only; exported for compression.py (Task 8) reuse.

def pick_interior_chunks(shape, chunks, n) -> list[tuple]:
    """Chunk grid indices nearest the array centroid, spiraling outward
    (Chebyshev shells), up to ``n`` candidates. For shape (100,100)
    chunks (10,10) the first candidate is the centroid chunk (5,5).
    """
    ndim = len(shape)
    grid = [math.ceil(s / c) for s, c in zip(shape, chunks)]
    center = tuple(g // 2 for g in grid)
    seen: set[tuple] = set()
    result: list[tuple] = []
    max_radius = sum(grid) if grid else 0
    radius = 0
    while len(result) < n and radius <= max_radius:
        ranges = []
        for i in range(ndim):
            lo = max(0, center[i] - radius)
            hi = min(grid[i] - 1, center[i] + radius)
            ranges.append(range(lo, hi + 1))
        candidates = []
        for point in itertools.product(*ranges):
            if point in seen:
                continue
            if radius == 0 or any(abs(point[i] - center[i]) == radius for i in range(ndim)):
                candidates.append(point)
        candidates.sort(key=lambda p: sum((p[i] - center[i]) ** 2 for i in range(ndim)))
        for c in candidates:
            if c in seen:
                continue
            seen.add(c)
            result.append(c)
            if len(result) >= n:
                break
        radius += 1
    return result[:n]


def is_data_bearing(decoded: "np.ndarray", fill_value=None, threshold: float = 0.9) -> bool:
    """Reject a decoded chunk whose fill/NaN fraction is >= ``threshold``
    (default 90%). NaN is always checked for floating/complex dtypes;
    ``fill_value`` (from a caller-supplied ``_FillValue`` attr or
    argument) is checked in addition when given.
    """
    arr = np.asarray(decoded)
    total = arr.size
    if total == 0:
        return False
    if np.issubdtype(arr.dtype, np.complexfloating):
        mask = np.isnan(arr.real) | np.isnan(arr.imag)
    elif np.issubdtype(arr.dtype, np.floating):
        mask = np.isnan(arr)
    else:
        mask = np.zeros(arr.shape, dtype=bool)
    if fill_value is not None:
        with np.errstate(invalid="ignore"):
            mask = mask | (arr == fill_value)
    frac_fill = mask.sum() / total
    return frac_fill < threshold


def _scan_interior(candidates, lookup_size, decode, fill_value, n):
    """Shared scan loop for zarr/HDF5 samplers: walk ``candidates``
    (chunk grid indices), skip ones with no stored size (unwritten/
    sparse), decode the rest and keep only data-bearing ones (S4) up to
    ``n``. Returns ``(databearing_sizes, all_sizes_seen, tried)`` so the
    caller can still report sizes (with ``data_bearing: false``) if
    every candidate was fill.
    """
    databearing_sizes = []
    all_sizes = []
    tried = 0
    for idx in candidates:
        size = lookup_size(idx)
        if size is None:
            continue
        tried += 1
        try:
            decoded = decode(idx)
        except Exception:
            continue
        if size:
            all_sizes.append(size)
        if is_data_bearing(decoded, fill_value=fill_value):
            if size:
                databearing_sizes.append(size)
            if len(databearing_sizes) >= n:
                break
    return databearing_sizes, all_sizes[:n], tried


_ALL_FILL_NOTE = ("all sampled chunks >=90% fill - compression/throughput telemetry "
                  "unreliable")


def _finish_sample(databearing_sizes, all_sizes, tried):
    if databearing_sizes:
        return {"sizes": databearing_sizes, "measured": True,
                "data_bearing": True, "note": None}
    if tried == 0:
        return {"sizes": [], "measured": False, "data_bearing": None,
                "note": "no stored chunks found near interior candidates"}
    return {"sizes": all_sizes, "measured": bool(all_sizes),
            "data_bearing": False, "note": _ALL_FILL_NOTE}


# ------------------------------------------------------------ zarr chunk keys

_ZARR_META_BASENAMES = {".zarray", ".zattrs", ".zgroup", ".zmetadata", "zarr.json"}


def _parse_zarr_chunk_key(rel: str):
    rel = rel.strip("/")
    if not rel:
        return None
    if rel.startswith("c/"):
        parts = rel[2:].split("/")
    else:
        parts = rel.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _sample_zarr_chunk_sizes(arr, fs, array_path, n):
    shape = tuple(arr.shape)
    chunks = tuple(getattr(arr, "chunks", ()) or ())
    if not chunks or not shape:
        return {"sizes": [], "measured": False, "data_bearing": None,
                "note": "no chunk grid (scalar or unchunked array)"}

    prefix = array_path.rstrip("/") + "/"
    try:
        listed = fs.find(prefix, detail=True)
    except Exception as e:
        return {"sizes": [], "measured": False, "data_bearing": None,
                "note": f"could not list stored chunks: {type(e).__name__}: {e}"}

    key_to_size: dict[tuple, int | None] = {}
    for key, info in listed.items():
        base = key.rsplit("/", 1)[-1]
        if base in _ZARR_META_BASENAMES:
            continue
        rel = key[len(prefix):] if key.startswith(prefix) else key
        idx = _parse_zarr_chunk_key(rel)
        if idx is not None:
            key_to_size[idx] = info.get("size")

    fill_value = getattr(arr, "fill_value", None)
    candidates = pick_interior_chunks(shape, chunks, max(n, 1) * 4)

    def lookup_size(idx):
        return key_to_size.get(idx)

    def decode(idx):
        slices = tuple(slice(i * c, min(s, (i + 1) * c))
                        for i, c, s in zip(idx, chunks, shape))
        return np.asarray(arr[slices])

    databearing, allsizes, tried = _scan_interior(candidates, lookup_size, decode,
                                                   fill_value, n)
    return _finish_sample(databearing, allsizes, tried)


# ------------------------------------------------------------ hdf5 chunk info

def _sample_hdf5_chunk_sizes(dataset, n):
    chunks = dataset.chunks
    shape = dataset.shape
    if not chunks:
        note = ("contiguous (unchunked) - whole-variable reads only; no subsetting "
                 "benefit from compression" if dataset.compression else
                 "contiguous (unchunked) - strided range reads possible but no "
                 "chunk-level parallelism")
        return {"sizes": [], "measured": False, "data_bearing": None, "note": note}

    dsid = dataset.id
    fill_value = dataset.fillvalue
    candidates = pick_interior_chunks(shape, chunks, max(n, 1) * 4)

    def lookup_size(idx):
        start = tuple(i * c for i, c in zip(idx, chunks))
        try:
            info = dsid.get_chunk_info_by_coord(start)
        except Exception:
            return None
        return info.size or None

    def decode(idx):
        start = tuple(i * c for i, c in zip(idx, chunks))
        slices = tuple(slice(s, min(sh, s + c)) for s, c, sh in zip(start, chunks, shape))
        return np.asarray(dataset[slices])

    databearing, allsizes, tried = _scan_interior(candidates, lookup_size, decode,
                                                   fill_value, n)
    return _finish_sample(databearing, allsizes, tried)


def sample_chunk_sizes(handle_or_dsid, fs, path, *, engine, n=8) -> dict:
    """Measured stored (compressed) chunk sizes, sampled interior +
    data-bearing (S4). ``handle_or_dsid`` is the zarr Array or h5py
    Dataset object for the variable being sampled; for zarr, ``path``
    must be the *array's own* store path (root store path + variable
    name already joined by the caller) so stored chunk keys can be
    listed via ``fs.find``; for HDF5, ``fs``/``path`` are unused (chunk
    info comes from the dataset's own ``.id``).

    Returns ``{"sizes": [int, ...], "measured": bool,
    "data_bearing": bool|None, "note": str|None}``. ``measured`` is
    True whenever real stored sizes were found (even if none were
    data-bearing); it is False only when no chunk grid exists or no
    stored chunks could be found/listed at all.
    """
    if engine in ("zarr", "icechunk"):
        return _sample_zarr_chunk_sizes(handle_or_dsid, fs, path, n)
    if engine in ("hdf5", "netcdf4", "h5", "h5py"):
        return _sample_hdf5_chunk_sizes(handle_or_dsid, n)
    return {"sizes": [], "measured": False, "data_bearing": None,
            "note": f"no chunk sampler for engine {engine!r}"}


# -------------------------------------------------------------- orientation

_TIME_NAMES = {"time", "t", "date", "step", "epoch"}
_VERT_NAMES = {"level", "lev", "plev", "z", "depth", "height", "alt", "altitude"}
_Y_NAMES = {"lat", "latitude", "y", "northing", "rows"}
_X_NAMES = {"lon", "longitude", "x", "easting", "cols", "columns"}
_KNOWN_NAMES = _TIME_NAMES | _VERT_NAMES | _Y_NAMES | _X_NAMES

# Orientation classification thresholds (documented, not hidden magic
# numbers): a spatial dim's per-dim coverage fraction f_i = chunks_i/shape_i.
_SPATIAL_LARGE_F = 0.5   # every spatial dim >=50% covered -> "spatially large"
_SPATIAL_SMALL_F = 0.1   # every spatial dim <10% covered -> "small footprint"
_TIME_EXTENT_TS = 100    # time chunk extent >=100 steps -> timeseries signal
_F_TIME_TS = 0.5         # or time coverage fraction >=50% -> timeseries signal
_MUCH_LESS_FACTOR = 0.1  # f_time <= this * f_spatial_min counts as "time-thin"

_AMP_WARN = 10.0
_AMP_FAIL = 100.0

# declare_orientation doesn't receive a dtype (see its signature in the task
# brief), so "bytes needed" for its canonical queries uses this placeholder
# itemsize (float64-equivalent) rather than a real per-variable dtype. This
# only affects the amplification estimate's absolute scale, not its
# direction; assess_chunking's own compressed_chunk_mb measurement (which
# does know the real dtype) is what's actually graded via grade().
_PLACEHOLDER_ITEMSIZE_BYTES = 8


def _grade_amplification(amp):
    if amp is None:
        return None
    if amp > _AMP_FAIL:
        return "fail"
    if amp > _AMP_WARN:
        return "warn"
    return "pass"


def _classify_dims(dims):
    """Returns (time_idx, spatial_idx_list, hedged: bool). If no dim
    name is recognized at all, falls back to the positional convention
    (axis 0 = record/time-like dim, the rest = spatial) with
    ``hedged=True``.
    """
    time_idx = None
    spatial_idx = []
    any_known = False
    for i, d in enumerate(dims):
        dl = (d or "").lower()
        if dl in _TIME_NAMES and time_idx is None:
            time_idx = i
            any_known = True
        elif dl in _KNOWN_NAMES:
            spatial_idx.append(i)
            any_known = True
    if any_known:
        return time_idx, spatial_idx, False
    # positional fallback
    if len(dims) >= 2:
        return 0, list(range(1, len(dims))), True
    return None, list(range(len(dims))), True


def _query(chunks_touched, avg_chunk_mb, needed_bytes, use_case_target, use_case):
    bytes_transferred = chunks_touched * (avg_chunk_mb or 0) * 2**20
    amplification = (bytes_transferred / needed_bytes) if needed_bytes else None
    grade_val = _grade_amplification(amplification) if use_case == use_case_target else None
    return {
        "chunks_touched": chunks_touched,
        "bytes_transferred_estimate": round(bytes_transferred, 1),
        "bytes_needed_estimate": round(needed_bytes, 1),
        "amplification": round(amplification, 2) if amplification is not None else None,
        "grade": grade_val,
    }


def declare_orientation(dims: list[str], shape: list[int], chunks: list[int], *,
                         avg_chunk_mb: float | None = None,
                         use_case: str | None = None) -> dict:
    """Classify what this chunk layout is optimized for and compute the
    two canonical queries (full spatial extent at one time; full-depth
    series at one point). ``avg_chunk_mb`` (compressed, if known) feeds
    the amplification estimate; without it, amplification is computed
    against a 1 MB per-chunk placeholder (still comparable in shape,
    documented as such via ``bytes_transferred_estimate``).

    ``use_case`` (``"timeseries"|"maps"|None``) gates whether the
    relevant query's amplification is *graded* (warn >10x, fail >100x);
    it is always *reported* as evidence regardless.
    """
    ndim = len(dims)
    if ndim == 0 or not shape or not chunks:
        return {"orientation": "unknown",
                "prose": "unable to classify chunking orientation: insufficient "
                         "dimension information",
                "queries": {}}

    time_idx, spatial_idx, hedged = _classify_dims(dims)
    hedge_prefix = ("(dims are unnamed/unrecognized; assuming axis 0 is the record "
                    "dimension) ") if hedged else ""
    itemsize_mb = (avg_chunk_mb if avg_chunk_mb is not None
                   else _estimate_compressed_mb(chunks, "float32") or 1.0)

    if time_idx is None:
        # No time-like dim at all: pure spatial tiling description.
        n_total = _estimate_chunk_count(shape, chunks) or 1
        cy_cx = "x".join(str(chunks[i]) for i in spatial_idx) if spatial_idx else "?"
        prose = (f"{hedge_prefix}chunks are tiled {cy_cx} - efficient windowed spatial "
                 f"reads; full-scene reads touch {n_total} chunks")
        needed_bytes = math.prod(shape) * _PLACEHOLDER_ITEMSIZE_BYTES if shape else None
        query = {
            "chunks_touched": n_total,
            "bytes_transferred_estimate": round(n_total * itemsize_mb * 2**20, 1),
            "bytes_needed_estimate": round(needed_bytes, 1) if needed_bytes else None,
            "amplification": None,
            "grade": None,
        }
        return {"orientation": "tiled", "prose": prose,
                "queries": {"full_scene_read": query}}

    spatial_idx = [i for i in spatial_idx if i != time_idx] or \
        [i for i in range(ndim) if i != time_idx]

    time_extent = chunks[time_idx]
    f_time = time_extent / shape[time_idx] if shape[time_idx] else 0.0
    spatial_fracs = [chunks[i] / shape[i] for i in spatial_idx if shape[i]]
    f_spatial_min = min(spatial_fracs) if spatial_fracs else 0.0
    f_spatial_max = max(spatial_fracs) if spatial_fracs else 0.0

    spatial_large = f_spatial_min >= _SPATIAL_LARGE_F
    spatial_small = f_spatial_max < _SPATIAL_SMALL_F
    time_thin = time_extent == 1 or f_time <= _MUCH_LESS_FACTOR * max(f_spatial_min, 1e-12)
    map_cond = spatial_large and time_thin
    ts_cond = spatial_small and (time_extent >= _TIME_EXTENT_TS or f_time >= _F_TIME_TS)

    map_chunks_touched = math.prod(math.ceil(shape[i] / chunks[i]) for i in spatial_idx)
    series_chunks_touched = math.ceil(shape[time_idx] / chunks[time_idx])

    map_needed_bytes = math.prod(shape[i] for i in spatial_idx) * _PLACEHOLDER_ITEMSIZE_BYTES
    series_needed_bytes = shape[time_idx] * _PLACEHOLDER_ITEMSIZE_BYTES

    queries = {
        "map_full_extent_one_time": _query(
            map_chunks_touched, itemsize_mb, map_needed_bytes, "maps", use_case),
        "timeseries_full_depth_one_point": _query(
            series_chunks_touched, itemsize_mb, series_needed_bytes, "timeseries", use_case),
    }

    if map_cond:
        orientation = "map-optimized"
        prose = (f"{hedge_prefix}chunks are time-thin and spatially large -> optimized "
                 f"for full-extent maps at a single time step; poor for time series at "
                 f"a point (a full series touches {series_chunks_touched} chunks)")
    elif ts_cond:
        orientation = "timeseries-optimized"
        prose = (f"{hedge_prefix}chunks span many time steps -> optimized for timeseries "
                 f"reads at a point; poor for full-extent spatial maps (one map touches "
                 f"{map_chunks_touched} chunks)")
    else:
        orientation = "balanced"
        prose = (f"{hedge_prefix}chunks favor neither full-extent maps nor point time "
                 f"series; a full-extent map touches {map_chunks_touched} chunks and a "
                 f"full-depth series at a point touches {series_chunks_touched} chunks - "
                 "reasonable middle ground; if both access patterns matter heavily, "
                 "consider a dual-copy layout (one map-optimized, one timeseries-optimized)")

    return {"orientation": orientation, "prose": prose, "queries": queries}


# ---------------------------------------------------- coordinate/contiguous notes

def _coordinate_chunking_notes(name, dims, shape, chunks):
    notes = []
    if chunks and shape and len(shape) == 1 and shape[0]:
        dim0 = (dims[0].lower() if dims else "")
        basename = name.rsplit("/", 1)[-1].lower()
        is_coordish = dim0 in _KNOWN_NAMES or basename == dim0 or basename in _KNOWN_NAMES
        if is_coordish:
            n_chunks = math.ceil(shape[0] / chunks[0])
            whole_mb = shape[0] * 8 / 2**20  # worst-case 8-byte coordinate dtype
            if n_chunks > 1 and whole_mb < 8:
                notes.append(
                    f"1-D coordinate array split into {n_chunks} chunks; coordinates are "
                    "typically read whole - a single chunk (or larger chunk size) avoids "
                    "needless request fan-out")
    return notes


def _contiguous_note(codec):
    return ("contiguous (unchunked) - whole-variable reads only; no subsetting benefit "
            "from compression" if codec else
            "contiguous (unchunked) - strided range reads possible but no chunk-level "
            "parallelism")


# ------------------------------------------------------------------ profiles

_PROFILE_TARGETS = {
    "interactive": (1.0, 4.0),
    "training": (10.0, 100.0),
    "agentic": (1.0, 16.0),
}


def _profile_status(compressed_mb, lo, hi):
    if compressed_mb is None:
        return "unknown"
    if compressed_mb < lo:
        return "below"
    if compressed_mb > hi:
        return "above"
    return "within"


def _build_profiles(compressed_mb):
    profiles = {}
    for profile, (lo, hi) in _PROFILE_TARGETS.items():
        entry = {"target_mb": [lo, hi], "status": _profile_status(compressed_mb, lo, hi)}
        if profile == "training":
            entry["sweet_spot_mb"] = [32.0, 64.0]
        if profile == "agentic":
            entry["note"] = "schema must additionally be enumerable in <=1 request"
        profiles[profile] = entry
    return profiles


# ------------------------------------------------------------------ lookup

def _lookup_handle(handle, name, engine):
    key = name.lstrip("/")
    if engine in ("zarr", "icechunk"):
        return handle[key] if key else handle
    return handle[key]


def _compressed_mb_stats(sizes):
    import statistics
    mb = [s / 2**20 for s in sizes]
    return {"median": round(statistics.median(mb), 3), "min": round(min(mb), 3),
            "max": round(max(mb), 3), "n_sampled": len(mb)}


# --------------------------------------------------------------- assess_chunking

def assess_chunking(handle, fs, path, variables: list[dict], *, budget,
                     use_case: str | None = None, engine: str) -> dict:
    """Per-variable chunk measurement, grading, orientation, and the
    three-profile informational overlay. ``variables`` are Ruling I-1
    inventory records (name/dims/shape/dtype/chunks/attrs/size_bytes/
    codec) -- typically ``OpenResult["inventory"]``, augmented with a
    ``dims`` list by the caller since openers only fills in
    ``dim_0``/``dim_1``/... placeholders.
    """
    out_variables = []
    for rec in variables:
        name = rec["name"]
        dims = rec.get("dims") or []
        shape = rec.get("shape") or []
        chunks = rec.get("chunks")
        dtype = rec.get("dtype", "float64")
        codec = rec.get("codec")
        notes: list[str] = []

        if not chunks:
            notes.append(_contiguous_note(codec))
            compressed_mb = _estimate_compressed_mb(shape, dtype) if shape else None
            compressed_info = {"estimated_median": compressed_mb, "ratio_assumed": _ASSUMED_RATIO}
            measured = False
            orientation = None
        else:
            measured = False
            compressed_info = None
            sample = None
            try:
                obj = _lookup_handle(handle, name, engine)
                array_path = f"{str(path).rstrip('/')}/{name.lstrip('/')}"
                sample = sample_chunk_sizes(obj, fs, array_path, engine=engine, n=8)
            except Exception as e:
                notes.append(f"chunk sampling failed: {type(e).__name__}: {e}")

            if sample and sample.get("sizes"):
                measured = True
                compressed_info = _compressed_mb_stats(sample["sizes"])
                if sample.get("data_bearing") is False and sample.get("note"):
                    notes.append(sample["note"])
            else:
                if sample and sample.get("note"):
                    notes.append(sample["note"])
                est = _estimate_compressed_mb(chunks, dtype)
                compressed_info = {"estimated_median": est, "ratio_assumed": _ASSUMED_RATIO}

            avg_mb = (compressed_info.get("median") if measured
                      else compressed_info.get("estimated_median"))
            orientation = declare_orientation(dims, shape, chunks, avg_chunk_mb=avg_mb,
                                               use_case=use_case)

        avg_mb = (compressed_info.get("median") if measured
                  else compressed_info.get("estimated_median")) if compressed_info else None
        grade_band, grade_note = grade(avg_mb)
        notes.extend(_coordinate_chunking_notes(name, dims, shape, chunks))

        out_variables.append({
            "name": name,
            "chunks": chunks,
            "shape": shape,
            "measured": measured,
            "compressed_chunk_mb": compressed_info,
            "grade": grade_band,
            "grade_note": grade_note,
            "orientation": orientation,
            "profiles": _build_profiles(avg_mb),
            "coordinate_chunking_notes": notes,
        })

    if budget is not None:
        try:
            budget.check_stage("chunk_sampling", raise_on_breach=False)
        except Exception:
            pass

    return {"variables": out_variables}
