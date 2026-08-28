# Conventions checks (CF + GeoZarr)

This is the spec `conventions.py`'s `check_cf`/`check_geozarr` implement — both run
automatically whenever variables are selected; there's nothing to opt into. Both
checks answer the same underlying question: **can standard tools interpret this data
without human archaeology?** Grade on interpretability, not letter-of-the-spec
pedantry — a missing `Conventions` attribute on an otherwise CF-shaped dataset is a
WARN with a one-line fix, not a FAIL.

## CF conventions (`check_cf`)

A pure structural pass over already-collected variable attrs (no xarray/network
dependency — the attrs `openers.py` already captured answer everything here):

- `Conventions` global attribute present and naming a CF version?
- Every data variable (i.e. not a coordinate, CRS-container, or bounds variable) has
  `units` and `standard_name` or at least `long_name`?
- Coordinate variables identifiable: by name (`time`, `lat`/`latitude`,
  `lon`/`longitude`, ...), by CF `axis` (T/X/Y/Z), or by the netCDF-Java/THREDDS
  `_CoordinateAxisType` convention — any one of these signals is sufficient. Time
  coordinates additionally need `<t> since ...`-style `units`; lat/lon need degree
  units or a matching `standard_name`/axis-type. Auxiliary coordinates named in a
  `coordinates` attribute must resolve to an actual variable.
- `grid_mapping` variable present and referenced, **or** — when no explicit
  `grid_mapping` attribute exists anywhere — a recognizable CRS-container variable by
  name (`projection`, `crs`, `spatial_ref`). NISAR-style products commonly carry CRS
  information in a `projection` dataset with no CF `grid_mapping` link at all; this
  fallback credits that as CRS-present evidence (explicitly noted as "inferred from
  name, not an explicit CF link", not silently treated as a full CF pass).
- `_FillValue`/`missing_value` present and consistent (same value if both given; in
  dtype range for integer types); `scale_factor`/`add_offset` numerically typed.
- `bounds` attributes (cell bounds variables) resolve to an actual variable.

**Rollup**: if every variable's attrs and the dataset's global attrs are both
completely empty, status is `skipped` ("attributes unavailable") — not a false
negative. Otherwise: all applicable checks pass → `pass`; a mix → `warn` ("core
present but gaps"); none pass → `fail` ("no CF signal at all" — note CF may simply
not apply to this format; the caller decides applicability, this function doesn't).

A formal pass (only if the user wants it, outside this CLI): IOOS
`compliance-checker` (`pip install compliance-checker`;
`compliance-checker --test cf:1.8 <file>`) works on NetCDF files; for Zarr, this
structural pass is the substitute, or export a subset to NetCDF first.

## GeoZarr (`check_geozarr`, Zarr/Icechunk stores only)

The GeoZarr spec (Zarr conventions for geospatial rasters) is still maturing —
`check_geozarr`'s status is only ever `pass` or `warn`, never `fail`; absence is
never treated as a hard failure. It is layout-level (no rasterio) and
listing-free — reading just consolidated metadata (Zarr v3's `zarr.json`
`consolidated_metadata`, or v2's `.zmetadata`) if present, since a single read then
reveals every array's attrs at once. Without consolidated metadata, only the root
group's own attrs are visible (that gap is itself one of this check's signals).

Checks:

- **Consolidated metadata present** — also feeds the metadata-locality check.
- **`_ARRAY_DIMENSIONS` (v2/xarray) or v3 `dimension_names` declared** on every
  non-CRS-container array, so a reader can label dimensions without guessing.
- **Multiscales** pyramid declared (root or per-array `multiscales` key) — absence is
  only a gap when visualization is a stated use case, not evaluated as a hard signal
  here.
- The CF-shaped parts (units/standard_name, grid_mapping) are **delegated to
  `check_cf`** against a pseudo-inventory built from the per-array attrs found — if
  the store fails the CF-ish parts, it fails GeoZarr too; this check doesn't
  double-report the same root cause under a different name.
