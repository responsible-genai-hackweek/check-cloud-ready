# Conventions checks (optional)

Both checks answer the same underlying question: **can standard tools interpret
this data without human archaeology?** Grade on interpretability, not
letter-of-the-spec pedantry — a missing `Conventions` attribute on an otherwise
perfectly CF-shaped dataset is a WARN with a one-line fix, not a FAIL.

## CF conventions

Quick structural pass (no extra deps beyond xarray):

- `Conventions` global attribute present and naming a CF version?
- Every assessed variable has `units` (UDUNITS-parsable) and `standard_name`
  (in the CF standard-name table) or at least `long_name`?
- Coordinate variables identifiable: time with proper `units` ("<t> since ..."),
  `calendar`; lat/lon with `units`/`standard_name`; `axis` or `_CoordinateAxisType`
  hints; auxiliary coordinates listed in `coordinates` attributes?
- Projected data: `grid_mapping` variable present and referenced?
- `_FillValue`/`missing_value` consistent with dtype; `scale_factor`/`add_offset`
  typed correctly?
- `cf_xarray` is a good probe: if `ds.cf` can identify latitude, longitude, time,
  and vertical axes, most downstream tooling will cope.

Formal pass (only if the user wants it): IOOS `compliance-checker`
(`pip install compliance-checker`; `compliance-checker --test cf:1.8 <file>`)
gives a scored report — works on NetCDF files; for Zarr, run the structural pass
above instead or export a small subset to NetCDF first.

Report the handful of highest-impact violations with fixes, not the full log.

## GeoZarr (Zarr stores only)

The GeoZarr spec (zarr conventions for geospatial rasters) is still maturing —
say so, and frame this check as "forward conformance". Check:

- CF-style attributes on arrays (the spec builds on CF): `standard_name`,
  `units`, coordinate identification via `_ARRAY_DIMENSIONS` (v2/xarray
  convention) or v3 `dimension_names`.
- CRS declared: a `grid_mapping` variable / CF grid-mapping attributes, or
  explicit spatial-ref metadata a reader like rioxarray can find.
- Multiscales: if the data serves visualization, is there a multiscale pyramid
  declared per the multiscales convention? Absence is only a WARN when
  visualization is a stated use case.
- Consolidated metadata present (also feeds the chunking/metadata check).

If the store fails the CF-ish parts, it fails GeoZarr too — run the CF check
first and don't double-report the same root cause.
