"""CF structural checks + GeoZarr forward-conformance checks.

Turns the prose heuristics of
``.claude/skills/check-cloud-ready/references/conventions.md`` into
code. Both checks answer the same underlying question -- can standard
tools interpret this data without human archaeology? -- and grade on
interpretability, not letter-of-the-spec pedantry: a missing
``Conventions`` attribute on an otherwise CF-shaped dataset is a WARN
with a one-line fix, never a hard FAIL (conventions.md, verbatim).

``check_cf`` is pure: it operates only on already-collected inventory
records (Ruling I-1: name/dims/shape/dtype/chunks/attrs/size_bytes/
codec) and a global-attrs dict -- no I/O, no xarray dependency (the
attrs already captured by ``openers.py`` answer everything
conventions.md's structural pass asks for).

``check_geozarr`` is zarr-only and layout-level (no rasterio). It reads
just enough store metadata to answer conventions.md's GeoZarr
questions -- consolidated metadata (a single read reveals every
array's attrs at once, so no store listing/``fs.find`` is required;
see ``_collect_zarr_meta``), ``_ARRAY_DIMENSIONS``/v3 ``dimension_names``,
and a multiscales declaration -- then delegates the CF-shaped parts
(units/standard_name, grid_mapping) to ``check_cf`` itself
(conventions.md: "If the store fails the CF-ish parts, it fails
GeoZarr too -- run the CF check first and don't double-report the same
root cause"). GeoZarr is a maturing, optional spec (conventions.md), so
its absence is never a hard fail -- ``check_geozarr``'s status is only
ever ``"pass"`` or ``"warn"``.
"""
from __future__ import annotations

import json
import re
from typing import Any

import numpy as np

__all__ = ["check_cf", "check_geozarr"]

# --------------------------------------------------------------- name heuristics

_COMMON_COORD_NAMES = {
    "time", "lat", "latitude", "lon", "longitude", "x", "y", "z",
    "level", "lev", "plev", "depth", "height",
}
_LAT_NAMES = {"lat", "latitude"}
_LON_NAMES = {"lon", "longitude"}
_TIME_NAMES = {"time"}
_CRS_CONTAINER_NAMES = {"projection", "crs", "spatial_ref"}

# CF `axis` values (case-insensitive) and the netCDF-Java/THREDDS
# `_CoordinateAxisType` convention -- both are additional coordinate-
# identification hints conventions.md calls out alongside name/
# standard_name matching.
_AXIS_VALUES = {"t", "x", "y", "z"}
_COORD_AXIS_TYPE_TIME = {"time"}
_COORD_AXIS_TYPE_LAT = {"lat", "latitude"}
_COORD_AXIS_TYPE_LON = {"lon", "longitude"}

_SINCE_RE = re.compile(r"\bsince\b", re.IGNORECASE)
_CF_VERSION_RE = re.compile(r"CF[- ]?(\d+(?:\.\d+)?)", re.IGNORECASE)


def _basename(name: str) -> str:
    return (name or "").rsplit("/", 1)[-1]


def _is_coordinate_var(v: dict) -> bool:
    """A variable is coordinate-like if its own (base)name is one of
    its declared dims (the classic netCDF coordinate-variable
    convention), a common coordinate name, or it carries an explicit
    `axis` (T/X/Y/Z) or `_CoordinateAxisType` attribute -- either of
    which identifies a variable as a coordinate regardless of its
    name."""
    base = _basename(v.get("name", ""))
    dims = v.get("dims") or []
    attrs = v.get("attrs") or {}
    if base in dims:
        return True
    if base.lower() in _COMMON_COORD_NAMES:
        return True
    if str(attrs.get("axis") or "").strip().lower() in _AXIS_VALUES:
        return True
    if attrs.get("_CoordinateAxisType"):
        return True
    return False


def _is_crs_container(v: dict) -> bool:
    return _basename(v.get("name", "")).lower() in _CRS_CONTAINER_NAMES


def _is_bounds_var(v: dict) -> bool:
    base = _basename(v.get("name", "")).lower()
    return "bnds" in base or "bounds" in base


def _data_variables(variables: list[dict]) -> list[dict]:
    """Variables that should be held to the "units + standard_name/
    long_name" bar: not coordinates, not CRS containers, not bounds
    variables."""
    return [v for v in variables
            if not _is_coordinate_var(v) and not _is_crs_container(v)
            and not _is_bounds_var(v)]


def _resolves(ref: str, by_name: set, by_basename: dict) -> bool:
    ref = str(ref)
    return ref in by_name or _basename(ref) in by_basename


def _name_lookup(variables: list[dict]):
    by_name = {v["name"] for v in variables}
    by_basename: dict[str, str] = {}
    for v in variables:
        by_basename.setdefault(_basename(v["name"]), v["name"])
    return by_name, by_basename


# --------------------------------------------------------------- CF: checks

def _check_conventions_attr(global_attrs: dict) -> dict:
    conv = (global_attrs or {}).get("Conventions") or (global_attrs or {}).get("conventions")
    if not conv:
        return {"id": "conventions_attr", "ok": False,
                "evidence": "no global Conventions attribute present"}
    m = _CF_VERSION_RE.search(str(conv))
    if m:
        return {"id": "conventions_attr", "ok": True,
                "evidence": f"Conventions={conv!r} names CF {m.group(1)}"}
    return {"id": "conventions_attr", "ok": False,
            "evidence": f"Conventions={conv!r} does not mention CF"}


def _check_units_and_name(variables: list[dict]) -> dict:
    data_vars = _data_variables(variables)
    if not data_vars:
        return {"id": "units_and_name", "ok": None,
                "evidence": "no data variables to check (only coordinates/CRS/bounds "
                            "found)"}
    missing = []
    for v in data_vars:
        attrs = v.get("attrs") or {}
        has_units = bool(attrs.get("units"))
        has_name = bool(attrs.get("standard_name") or attrs.get("long_name"))
        if not (has_units and has_name):
            reasons = []
            if not has_units:
                reasons.append("units")
            if not has_name:
                reasons.append("standard_name/long_name")
            missing.append(f"{v['name']} missing {' and '.join(reasons)}")
    if missing:
        return {"id": "units_and_name", "ok": False, "evidence": "; ".join(missing)}
    return {"id": "units_and_name", "ok": True,
            "evidence": f"all {len(data_vars)} data variable(s) have units and "
                        "standard_name/long_name"}


def _classify_coord_kind(base: str, std: str, axis: str, coord_axis_type: str) -> str:
    """Classify a coordinate-like variable as time/latitude/longitude/
    vertical/generic, using -- in order -- its (base)name, its
    `standard_name`, its CF `axis` value, and the netCDF-Java
    `_CoordinateAxisType` convention. Any one of these four signals is
    sufficient (conventions.md lists all four as valid identification
    hints, not just name/standard_name)."""
    if base in _TIME_NAMES or std == "time" or axis == "t" or coord_axis_type in _COORD_AXIS_TYPE_TIME:
        return "time"
    if base in _LAT_NAMES or std == "latitude" or axis == "y" or coord_axis_type in _COORD_AXIS_TYPE_LAT:
        return "latitude"
    if base in _LON_NAMES or std == "longitude" or axis == "x" or coord_axis_type in _COORD_AXIS_TYPE_LON:
        return "longitude"
    if axis == "z":
        return "vertical"
    if coord_axis_type and coord_axis_type not in (
            _COORD_AXIS_TYPE_TIME | _COORD_AXIS_TYPE_LAT | _COORD_AXIS_TYPE_LON):
        # Any other non-empty, unrecognized _CoordinateAxisType value
        # (e.g. "GeoZ", "Height", "Pressure") is treated as a generic
        # vertical/other coordinate hint.
        return "vertical"
    return "generic"


def _check_coordinate_identification(variables: list[dict]) -> dict:
    by_name, by_basename = _name_lookup(variables)
    coord_vars = [v for v in variables if _is_coordinate_var(v)]
    has_coordinates_attr = any((v.get("attrs") or {}).get("coordinates") for v in variables)

    if not coord_vars and not has_coordinates_attr:
        return {"id": "coordinate_identification", "ok": None,
                "evidence": "no coordinate-like variables or `coordinates` attributes "
                            "found"}

    problems = []
    identified = []

    for v in coord_vars:
        base = _basename(v["name"]).lower()
        attrs = v.get("attrs") or {}
        units = str(attrs.get("units") or "")
        std = str(attrs.get("standard_name") or "").lower()
        axis = str(attrs.get("axis") or "").strip().lower()
        coord_axis_type = str(attrs.get("_CoordinateAxisType") or "").strip().lower()
        hint = ""
        if attrs.get("axis"):
            hint += f", axis={attrs.get('axis')!r}"
        if attrs.get("_CoordinateAxisType"):
            hint += f", _CoordinateAxisType={attrs.get('_CoordinateAxisType')!r}"

        kind = _classify_coord_kind(base, std, axis, coord_axis_type)
        if kind == "time":
            if _SINCE_RE.search(units):
                identified.append(
                    f"{v['name']} (time; units={units!r}, calendar="
                    f"{attrs.get('calendar', 'unspecified')!r}{hint})")
            else:
                problems.append(
                    f"{v['name']} looks like a time coordinate but units {units!r} "
                    f"lack a '<t> since ...' reference{hint}")
        elif kind == "latitude":
            if "degree" in units.lower() or std == "latitude" or coord_axis_type in _COORD_AXIS_TYPE_LAT:
                identified.append(f"{v['name']} (latitude; units={units!r}{hint})")
            else:
                problems.append(
                    f"{v['name']} looks like latitude but has no degrees units or "
                    f"latitude standard_name{hint}")
        elif kind == "longitude":
            if "degree" in units.lower() or std == "longitude" or coord_axis_type in _COORD_AXIS_TYPE_LON:
                identified.append(f"{v['name']} (longitude; units={units!r}{hint})")
            else:
                problems.append(
                    f"{v['name']} looks like longitude but has no degrees units or "
                    f"longitude standard_name{hint}")
        elif kind == "vertical":
            identified.append(
                f"{v['name']} (vertical/other coordinate, identified via axis/"
                f"_CoordinateAxisType hint{hint})")
        else:
            identified.append(f"{v['name']} (coordinate)")

    # Auxiliary coordinates: any variable's `coordinates` attribute
    # (space-separated variable names, per CF) must resolve against
    # other inventory records -- reuse the same name-resolution
    # machinery `grid_mapping`/`bounds` already use.
    for v in variables:
        coords_attr = (v.get("attrs") or {}).get("coordinates")
        if not coords_attr:
            continue
        for token in str(coords_attr).split():
            if _resolves(token, by_name, by_basename):
                identified.append(f"{v['name']}.coordinates -> {token}")
            else:
                problems.append(
                    f"{v['name']} coordinates={coords_attr!r} references {token!r} "
                    "which does not resolve to any variable in the inventory")

    if problems:
        return {"id": "coordinate_identification", "ok": False, "evidence": "; ".join(problems)}
    return {"id": "coordinate_identification", "ok": True, "evidence": "; ".join(identified)}


def _check_grid_mapping(variables: list[dict]) -> dict:
    by_name, by_basename = _name_lookup(variables)
    refs = [(v, v.get("attrs", {}).get("grid_mapping")) for v in variables
            if (v.get("attrs") or {}).get("grid_mapping")]

    if refs:
        resolved, danglers = [], []
        for v, gm in refs:
            if _resolves(gm, by_name, by_basename):
                resolved.append(f"{v['name']} -> {gm}")
            else:
                danglers.append(f"{v['name']} grid_mapping={gm!r} does not resolve to "
                                 "any variable in the inventory")
        if danglers:
            return {"id": "grid_mapping", "ok": False, "evidence": "; ".join(danglers)}
        return {"id": "grid_mapping", "ok": True, "evidence": "; ".join(resolved)}

    # No explicit grid_mapping attribute anywhere: fall back to the
    # presence of a recognizable CRS-container variable (S6-style
    # detection by name) -- e.g. a NISAR-like "projection" dataset with
    # a captured WKT/spatial_ref attribute but no CF grid_mapping link.
    containers = [v["name"] for v in variables if _is_crs_container(v)]
    if containers:
        return {"id": "grid_mapping", "ok": True,
                "evidence": "no explicit grid_mapping attribute, but CRS container "
                            "variable(s) present: " + ", ".join(containers) +
                            " (inferred from name, not an explicit CF link)"}
    return {"id": "grid_mapping", "ok": None,
            "evidence": "no grid_mapping attribute or CRS container variable found"}


def _check_fill_value_consistency(variables: list[dict]) -> dict:
    checked = 0
    problems = []
    for v in variables:
        attrs = v.get("attrs") or {}
        fv = attrs.get("_FillValue")
        mv = attrs.get("missing_value")
        if fv is None and mv is None:
            continue
        checked += 1
        if fv is not None and mv is not None and fv != mv:
            problems.append(f"{v['name']}: _FillValue={fv!r} != missing_value={mv!r}")
            continue
        val = fv if fv is not None else mv
        dtype = v.get("dtype")
        if dtype:
            try:
                npdt = np.dtype(dtype)
                if np.issubdtype(npdt, np.integer):
                    info = np.iinfo(npdt)
                    if not (info.min <= val <= info.max):
                        problems.append(
                            f"{v['name']}: fill value {val!r} out of range for dtype "
                            f"{dtype}")
            except (TypeError, ValueError):
                pass
    if checked == 0:
        return {"id": "fill_value_consistency", "ok": None,
                "evidence": "no _FillValue/missing_value attributes present"}
    if problems:
        return {"id": "fill_value_consistency", "ok": False, "evidence": "; ".join(problems)}
    return {"id": "fill_value_consistency", "ok": True,
            "evidence": f"{checked} variable(s) with consistent _FillValue/missing_value"}


def _check_scale_offset_typing(variables: list[dict]) -> dict:
    """conventions.md's fill-value bullet also requires
    ``scale_factor``/``add_offset`` to be "typed correctly" -- CF
    requires these to be numeric (matching, or safely castable to, the
    packed variable's dtype); this checks the minimal, unambiguous
    part of that: are they numeric at all, not e.g. a stringly-typed
    value that would break unpacking (``unpacked = packed *
    scale_factor + add_offset``)."""
    checked = 0
    problems = []
    for v in variables:
        attrs = v.get("attrs") or {}
        for key in ("scale_factor", "add_offset"):
            val = attrs.get(key)
            if val is None:
                continue
            checked += 1
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                problems.append(
                    f"{v['name']}: {key}={val!r} is not numeric (type "
                    f"{type(val).__name__})")
    if checked == 0:
        return {"id": "scale_offset_typing", "ok": None,
                "evidence": "no scale_factor/add_offset attributes present"}
    if problems:
        return {"id": "scale_offset_typing", "ok": False, "evidence": "; ".join(problems)}
    return {"id": "scale_offset_typing", "ok": True,
            "evidence": f"{checked} scale_factor/add_offset attribute(s) numerically typed"}


def _check_bounds(variables: list[dict]) -> dict:
    by_name, by_basename = _name_lookup(variables)
    refs = [(v, v.get("attrs", {}).get("bounds")) for v in variables
            if (v.get("attrs") or {}).get("bounds")]
    if not refs:
        return {"id": "bounds", "ok": None, "evidence": "no bounds attributes present"}
    resolved, danglers = [], []
    for v, b in refs:
        if _resolves(b, by_name, by_basename):
            resolved.append(f"{v['name']} -> {b}")
        else:
            danglers.append(f"{v['name']} bounds={b!r} does not resolve to any variable "
                             "in the inventory")
    if danglers:
        return {"id": "bounds", "ok": False, "evidence": "; ".join(danglers)}
    return {"id": "bounds", "ok": True, "evidence": "; ".join(resolved)}


# Checks that take `global_attrs` vs. checks that take `variables` --
# `check_cf` builds its `checks` list from exactly these two registries
# (no separate inline duplicate of "which checks run").
_GLOBAL_ATTRS_CHECKS = (
    _check_conventions_attr,
)
_VARIABLES_CHECKS = (
    _check_units_and_name,
    _check_coordinate_identification,
    _check_grid_mapping,
    _check_fill_value_consistency,
    _check_scale_offset_typing,
    _check_bounds,
)


def check_cf(variables: list[dict], global_attrs: dict) -> dict:
    """Structural CF conventions pass over already-collected inventory
    records (Ruling I-1 schema) and a dataset's global attrs. No I/O.

    Returns ``{"status": "pass"|"warn"|"fail"|"skipped", "checks":
    [{"id", "ok": bool|None, "evidence"}, ...], "notes": [...]}``.

    ``ok`` is ``None`` when a check is not applicable (e.g. no
    coordinate-like variables were found to check at all) -- those
    checks are excluded from the pass/warn/fail rollup.

    Rollup: if every variable's ``attrs`` and ``global_attrs`` are both
    empty, there is nothing to go on at all -- status is ``"skipped"``
    ("attributes unavailable"), not a false negative. Otherwise: all
    applicable checks ok -> ``"pass"``; a mix of ok/not-ok -> ``"warn"``
    ("core present but gaps"); zero applicable checks ok -> ``"fail"``
    ("no CF signal at all"; CF conventions may simply not apply to this
    format -- the caller decides applicability).
    """
    global_attrs = global_attrs or {}
    any_attrs = bool(global_attrs) or any(v.get("attrs") for v in variables)

    checks = ([f(global_attrs) for f in _GLOBAL_ATTRS_CHECKS]
              + [f(variables) for f in _VARIABLES_CHECKS])

    if not any_attrs:
        return {"status": "skipped", "checks": checks,
                "notes": ["attributes unavailable: every variable's attrs and the "
                          "dataset's global attrs are empty"]}

    applicable = [c for c in checks if c["ok"] is not None]
    notes: list[str] = []
    if applicable and all(c["ok"] for c in applicable):
        status = "pass"
    elif applicable and any(c["ok"] for c in applicable):
        status = "warn"
    else:
        status = "fail"
        notes.append("no CF signal detected at all; CF conventions may not apply to "
                      "this format -- the caller decides whether that's expected")

    return {"status": status, "checks": checks, "notes": notes}


# ------------------------------------------------------------- GeoZarr: fs reads

def _try_json(raw: Any) -> dict:
    if raw is None:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _cat(fs, path: str):
    try:
        return fs.cat_file(path)
    except Exception:
        return None


def _collect_zarr_meta(fs, root: str) -> dict:
    """Bounded, listing-free zarr metadata probe: works against any
    fsspec-like fs exposing just ``.cat_file()`` -- a subset of the
    minimal surface ``formats.sniff_store`` requires (that function
    also uses ``.exists()``; this one never needs to, since every path
    it might read is instead attempted directly via ``.cat_file()``
    and treated as absent on any exception -- see ``_cat``).

    Consolidated metadata (zarr v3's ``zarr.json``
    ``consolidated_metadata``, or zarr v2's ``.zmetadata``) is
    deliberately preferred: a single read reveals every array's attrs
    at once, so no store listing (``fs.find``/``fs.ls``) is ever
    required here. Without consolidated metadata, only the root
    group's own attrs are visible (an unconsolidated store cannot be
    fully inspected without crawling it -- that gap is itself one of
    conventions.md's GeoZarr signals, reported via the
    ``consolidated_metadata`` check).

    Returns ``{"consolidated": bool, "root_attrs": dict,
    "arrays": {relpath: {"attrs": dict, "dimension_names": [str]|None}}}``.
    """
    base = root.rstrip("/")

    raw = _cat(fs, f"{base}/zarr.json")
    if raw is not None:
        doc = _try_json(raw)
        root_attrs = doc.get("attributes") or {}
        cm = doc.get("consolidated_metadata")
        arrays: dict[str, dict] = {}
        if cm and cm.get("metadata"):
            for key, meta in cm["metadata"].items():
                if not isinstance(meta, dict):
                    continue
                suffix = "/zarr.json"
                if key == "zarr.json":
                    continue  # the root entry itself, already read above
                if key.endswith(suffix):
                    relpath = key[: -len(suffix)]
                    arrays[relpath] = {
                        "attrs": meta.get("attributes") or {},
                        "dimension_names": meta.get("dimension_names"),
                    }
        return {"consolidated": bool(cm), "root_attrs": root_attrs, "arrays": arrays}

    raw = _cat(fs, f"{base}/.zmetadata")
    if raw is not None:
        doc = _try_json(raw)
        meta = doc.get("metadata") or {}
        root_attrs = meta.get(".zattrs") or {}
        arrays = {}
        for key, val in meta.items():
            if key == ".zattrs" or not isinstance(val, dict):
                continue
            if key.endswith("/.zattrs"):
                relpath = key[: -len("/.zattrs")]
                arrays.setdefault(relpath, {})["attrs"] = val
        return {"consolidated": True, "root_attrs": root_attrs, "arrays": arrays}

    # Unconsolidated: only the root group/array's own attrs are
    # reachable without a store listing.
    root_attrs = _try_json(_cat(fs, f"{base}/.zattrs"))
    return {"consolidated": False, "root_attrs": root_attrs, "arrays": {}}


# ------------------------------------------------------------- GeoZarr: checks

def _check_consolidated_metadata(consolidated: bool) -> dict:
    if consolidated:
        return {"id": "consolidated_metadata", "ok": True,
                "evidence": "consolidated metadata present"}
    return {"id": "consolidated_metadata", "ok": False,
            "evidence": "no consolidated metadata found; per-array attributes cannot "
                        "be fully inspected without crawling the store"}


def _check_dimension_names(arrays: dict) -> dict:
    # CRS-container arrays (e.g. a "crs"/"projection"/"spatial_ref"
    # scalar) are legitimately dimensionless -- only data-shaped arrays
    # are held to the _ARRAY_DIMENSIONS/dimension_names bar.
    checkable = {name: meta for name, meta in (arrays or {}).items()
                 if _basename(name).lower() not in _CRS_CONTAINER_NAMES}
    if not checkable:
        return {"id": "dimension_names", "ok": None,
                "evidence": "no per-array metadata available to check (consolidated "
                            "metadata absent, or store has no non-CRS-container "
                            "arrays)"}
    present, missing = [], []
    for name, meta in checkable.items():
        attrs = meta.get("attrs") or {}
        dn = meta.get("dimension_names") or attrs.get("_ARRAY_DIMENSIONS")
        (present if dn else missing).append(name)
    if not present:
        return {"id": "dimension_names", "ok": False,
                "evidence": f"no array declares _ARRAY_DIMENSIONS/dimension_names "
                            f"({len(missing)} array(s) checked)"}
    if missing:
        return {"id": "dimension_names", "ok": False,
                "evidence": f"{len(missing)} array(s) missing "
                            "_ARRAY_DIMENSIONS/dimension_names: " + ", ".join(sorted(missing))}
    return {"id": "dimension_names", "ok": True,
            "evidence": f"{len(present)} array(s) declare "
                        "_ARRAY_DIMENSIONS/dimension_names: " + ", ".join(sorted(present))}


def _check_multiscales(root_attrs: dict, arrays: dict) -> dict:
    if "multiscales" in (root_attrs or {}):
        return {"id": "multiscales", "ok": True,
                "evidence": "multiscales pyramid declared at the store root"}
    for name, meta in (arrays or {}).items():
        if "multiscales" in (meta.get("attrs") or {}):
            return {"id": "multiscales", "ok": True,
                    "evidence": f"multiscales pyramid declared on {name}"}
    return {"id": "multiscales", "ok": None,
            "evidence": "no multiscales pyramid declared (only a gap when "
                        "visualization is a stated use case; not evaluated here)"}


def _pseudo_inventory(arrays: dict) -> list[dict]:
    """Build minimal Ruling-I-1-shaped records from zarr array metadata
    so ``check_cf`` can be reused for the CF-shaped parts of the
    GeoZarr check (units/standard_name/grid_mapping) without
    duplicating that logic."""
    variables = []
    for name, meta in arrays.items():
        attrs = meta.get("attrs") or {}
        dims = meta.get("dimension_names") or attrs.get("_ARRAY_DIMENSIONS") or []
        variables.append({
            "name": "/" + str(name).lstrip("/"),
            "dims": list(dims),
            "shape": [],
            "dtype": None,
            "chunks": None,
            "attrs": attrs,
            "size_bytes": None,
            "codec": None,
        })
    return variables


def check_geozarr(fs, root: str, *, zarr_meta: dict | None = None) -> dict:
    """GeoZarr forward-conformance check (zarr stores only,
    layout-level -- no rasterio). ``fs``/``root`` are used the same way
    ``formats.sniff_store`` uses them -- a listing-free metadata probe,
    though this one only ever needs ``.cat_file()`` (see
    ``_collect_zarr_meta``); pass ``zarr_meta`` (the shape
    ``_collect_zarr_meta`` returns) to skip the fs probe entirely with
    already-collected metadata.

    GeoZarr is a maturing, optional spec (conventions.md): its absence
    is never a hard fail. This function's status is only ever
    ``"pass"`` or ``"warn"``.

    Delegates the CF-shaped parts (units/standard_name, grid_mapping)
    to ``check_cf`` against a pseudo-inventory built from the
    per-array attrs found, per conventions.md: "If the store fails the
    CF-ish parts, it fails GeoZarr too -- run the CF check first and
    don't double-report the same root cause."
    """
    if zarr_meta is None:
        zarr_meta = _collect_zarr_meta(fs, root)

    consolidated = bool(zarr_meta.get("consolidated"))
    root_attrs = zarr_meta.get("root_attrs") or {}
    arrays = zarr_meta.get("arrays") or {}

    pseudo_variables = _pseudo_inventory(arrays)
    cf_result = check_cf(pseudo_variables, root_attrs)

    checks = list(cf_result["checks"])
    checks.append(_check_consolidated_metadata(consolidated))
    checks.append(_check_dimension_names(arrays))
    checks.append(_check_multiscales(root_attrs, arrays))

    notes: list[str] = []
    if cf_result["status"] in ("fail", "warn") and cf_result.get("notes"):
        notes.append("GeoZarr builds on CF; see the CF-check notes above for the root "
                      "cause of any CF-shaped gaps")

    applicable = [c for c in checks if c["ok"] is not None]
    if applicable and all(c["ok"] for c in applicable):
        status = "pass"
    else:
        status = "warn"
        if not applicable or not any(c["ok"] for c in applicable):
            notes.append("not GeoZarr-conformant (optional spec)")

    return {"status": status, "checks": checks, "notes": notes}
