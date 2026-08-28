"""Rubric checks, dimension rollup, tier, and categorical verdict.

Pure function library: findings in (a plain ``asset`` dict assembled by
the future CLI orchestrator from Task 4/6/7/8/9's outputs) -> checks,
dimension scores, a 0-100 score, an A-F tier, a categorical verdict, and
a confidence label out. No I/O, no network, no rendering, no imports
from any sibling module in this package -- this module is intentionally
loosely coupled from openers/chunking/compression/conventions/access: it
only reads the plain dicts those modules already produce (see the
``score()`` docstring for the exact shape expected under each key).

Ported from (read, not imported):

- ``.claude/skills/earth-science-cloud-readiness/scripts/assess.py``:
  ``C()`` (~l.488), ``score_asset()`` (~l.494-853), ``rollup_checks()``
  (~l.917), the ``CLOUD_NATIVE``/``CLOUD_OPTIMIZABLE``/``CLOUD_HOSTILE``
  class tables, the cloud-hostile migration-command dict, and ``TIERS``.
- ``.claude/skills/earth-science-cloud-readiness/references/rubric.md``:
  dimension weights (A30/B20/C25/D15/E10), sub-check points, tiers
  (90/75/55/35), and the hard rules (smoke FAIL caps the score at 74;
  SKIPPED downgrades confidence, never the score; every fail needs a
  remediation).
- ``.claude/skills/check-cloud-ready/SKILL.md`` (~l.226-230): the
  categorical verdict mapping (READY / READY WITH CAVEATS / NOT READY).

STAC/Croissant rework
----------------------
The ported ``score_asset()`` pulled B4-catalog and several E-checks
(E3-license, E4-citation, E5-contact, E6-usage, E7-applications) from a
STAC item / GeoCroissant record (``asset.get("stac_item")`` /
``asset.get("croissant")`` / ``_catalog_meta_from(...)``). This package
has no STAC/Croissant catalog concept at all -- inputs are a single
asset URL/granule ID, not a catalog crawl -- so those branches are
reworked as follows, per the task brief:

- **B4-catalog** becomes a pure file-attrs check: does the asset
  self-describe well enough, at the file level alone, to stand in for
  "catalog quality" (format identified, CF-shaped self-description,
  variable-level attrs present)? It is always assessable from the asset
  dict itself, EXCEPT when literally nothing is known (format unknown,
  no CF signal, no variable attrs at all) -- that residual case is
  ``"n/a"`` (nothing to evaluate), not ``"fail"`` (evaluated and found
  wanting), matching rubric.md's "n/a when no catalog exists
  (renormalize)" in spirit.
- **E3/E4/E5/E6/E7** become CMR-umm-json-driven: when the CLI resolved
  a NASA CMR granule ID (Task 4), it can pass through ``cmr_meta``, a
  plain dict this module expects in the ORIGINAL ``score_asset()``'s
  ``catalog_meta`` shape (``license``, ``doi``/``citation``, ``contact``,
  ``doc_links``, ``n_keywords``, ``description_len``) -- translating raw
  UMM-JSON (``License``/``DOI``/``ContactPersons``/``RelatedUrls``/
  ``ScienceKeywords``/``Abstract``) into this shape is the CLI
  orchestrator's job (Task 12), not this module's. When ``cmr_meta`` is
  ``None`` (no granule-ID input, or resolution didn't reach CMR), those
  five checks are ``"skipped"`` (low-confidence, per the brief's
  explicit test case), never ``"fail"`` -- a deliberate override of the
  ported code's literal behavior (which always failed E3 outright with
  no license, catalog or not); the brief's test list requires
  ``cmr_meta is None -> E3/E4 skipped``.

Wiring contract (asset dict)
-----------------------------
See ``score()``'s docstring for the full per-key shape this module
expects. Every key is read defensively (``.get(...)`` with dict/list
defaults) so a caller that hasn't populated a given upstream module's
output yet still gets a coherent (if lower-confidence) score rather than
a crash.
"""
from __future__ import annotations

import math

__all__ = ["score", "rollup_checks", "tier_for", "verdict_for"]

# ------------------------------------------------------------ class tables
# Ported verbatim from assess.py / formats.py (duplicated here, not
# imported: this module must stay import-free of every sibling module).
CLOUD_NATIVE = {"cog", "zarr", "icechunk", "parquet", "copc", "flatgeobuf", "pmtiles"}
CLOUD_OPTIMIZABLE = {"hdf5", "netcdf4", "las", "kerchunk"}
CLOUD_HOSTILE = {"netcdf3", "grib2", "shapefile", "csv", "zip", "tar", "gzip", "hdf4"}

TIERS = [(90, "A"), (75, "B"), (55, "C"), (35, "D"), (0, "F")]

# Ported verbatim from assess.py's score_asset() migration-command dict.
_MIGRATIONS = {
    "netcdf3": "MANDATORY migration: `nccopy -k nc4 -d 4 -c time/1,lat/512,"
               "lon/512 in.nc out.nc4` or xarray-`to_zarr(zarr_format=3, "
               "consolidated=True)` with chunks from chunking-for-ai.md; "
               "interim: kerchunk virtual Zarr",
    "grib2": "MANDATORY migration: publish kerchunk refs "
             "(`kerchunk.grib2.scan_grib`) now; convert via cfgrib-rechunk-"
             "`to_zarr` for durable fix; at minimum publish .idx sidecars",
    "shapefile": "MANDATORY migration: `ogr2ogr -f Parquet out.parquet in.shp`"
                 " (analysis) or `ogr2ogr -f FlatGeobuf out.fgb in.shp` (web)",
    "csv": "MANDATORY migration: `duckdb -c \"COPY (SELECT * FROM "
           "read_csv_auto('in.csv')) TO 'out.parquet' (FORMAT PARQUET, "
           "COMPRESSION ZSTD)\"`",
    "hdf4": "MANDATORY migration: `h4toh5convert in.hdf out.h5` then "
            "`h5repack -S PAGE -G 8388608 out.h5 out-cloud.h5`; for "
            "HDF4-EOS grids go straight to COG per subdataset: "
            "`gdal_translate -of COG HDF4_EOS:EOS_GRID:\"in.hdf\":grid:"
            "field out.tif`; multi-file collections -> consolidated Zarr "
            "via xarray (pyhdf/rioxarray reader -> to_zarr)",
    "zip": "MANDATORY migration: unpack and publish members natively on "
           "object storage; never leave data only inside archives",
}
_MIGRATIONS["tar"] = _MIGRATIONS["gzip"] = _MIGRATIONS["zip"]

_GENERIC_FAIL_REMEDIATION = ("remediation not captured for this failing check; "
                             "investigate the underlying finding")
_D_FALLBACK_REMEDIATION = {
    "D1-range": "enable/verify HTTP Range support (206 responses) on the data endpoint",
    "D2-auth": "document auth clearly; verify credentials / requester-pays configuration",
    "D3-https-cors": "serve over HTTPS with valid TLS; add CORS headers if browser use "
                     "is plausible",
    "D4-cleanpath": "remove redirect chains / HTML interstitials in front of data URLs",
}

# Checks whose FAIL a real consumer would actually hit (SKILL.md ~l.226-230):
# smoke fail (handled separately, via smoke_status), chunk-size fail
# (C1/C2), no range support (D1), a data-integrity flag (folded into
# smoke_status -- see module docstring / task report for why).
_CONSUMER_VISIBLE_FAIL_IDS = {"C1-interactive", "C2-training", "D1-range"}


# ------------------------------------------------------------------ helpers

def C(id_, dim, pts, awarded, status, evidence, remediation=None):
    """Build one check-dict. Ported verbatim from assess.py's ``C()``."""
    return {"id": id_, "dimension": dim, "points_possible": pts,
            "points_awarded": round(awarded, 1), "status": status,
            "evidence": evidence, "remediation": remediation}


def tier_for(score: float) -> str:
    """A-F tier for a 0-100 score. Boundaries per rubric.md section 7:
    90/75/55/35 (each cutoff inclusive of the lower letter)."""
    for cut, letter in TIERS:
        if score >= cut:
            return letter
    return "F"


def rollup_checks(checks):
    """Per-dimension renormalization, ``"n/a"`` checks excluded (ported
    verbatim from assess.py's ``rollup_checks()``). ``"skipped"`` checks
    are NOT excluded: rubric.md section 1 is explicit that a skipped
    check is "scored at its static-inference value where possible" and
    only excluded "otherwise" -- every skipped check this module builds
    already carries such a static value, so it stays in the denominator
    with that value; only the confidence label (not the score) reflects
    the skip. Returns ``(dimensions, score)`` per the task brief's
    signature (a superset of the ported function, which only returned
    ``dimensions`` and left the sum to its caller).
    """
    dims = {}
    for d, full in (("A", 30), ("B", 20), ("C", 25), ("D", 15), ("E", 10)):
        cs = [c for c in checks if c["dimension"] == d and c["status"] != "n/a"]
        poss = sum(c["points_possible"] for c in cs)
        got = sum(c["points_awarded"] for c in cs)
        dims[d] = {"score": round(got / poss * full, 1) if poss else 0.0, "max": full}
    total = round(sum(d["score"] for d in dims.values()), 1)
    return dims, total


def verdict_for(checks, smoke_status):
    """Categorical verdict per check-cloud-ready/SKILL.md ~l.226-230:
    NOT READY iff >=1 consumer-visible FAIL (smoke fail, chunk-size
    fail, no range support, or a data-integrity flag -- the last is
    folded into ``smoke_status == "fail"`` here, since the smoke test IS
    the live data-integrity/readability probe and this port has no
    separate integrity-flag input); READY WITH CAVEATS iff only
    warn/partial-caliber gaps remain; READY iff every assessable check
    passes.
    """
    if smoke_status == "fail":
        return ("NOT READY",
                "smoke test failed: the data could not be read/verified live")

    consumer_fails = [c for c in checks
                      if c["id"] in _CONSUMER_VISIBLE_FAIL_IDS and c["status"] == "fail"]
    if consumer_fails:
        names = ", ".join(c["id"] for c in consumer_fails)
        return ("NOT READY", f"consumer-visible failure(s): {names}")

    caveats = [c for c in checks if c["status"] in ("partial", "fail")]
    if caveats:
        return ("READY WITH CAVEATS",
                "no consumer-blocking failures, but some checks warn/partial")

    return ("READY", "all assessed criteria pass")


# --------------------------------------------------------- A: format & structure

def _a_class_check(asset, fmt):
    open_ = asset.get("open") or {}
    fc = open_.get("format_checks") or {}
    inv = open_.get("inventory") or []

    cls = asset.get("format_class")
    if fmt in CLOUD_NATIVE:
        base, cls = 26, cls or "cloud-native"
    elif fmt in CLOUD_OPTIMIZABLE:
        base, cls = 14, cls or "cloud-optimizable"
    elif fmt in CLOUD_HOSTILE:
        base, cls = 4, cls or "cloud-hostile"
    else:
        base, cls = 10, cls or "unknown"

    adj, evid, rem = 0.0, [f"format={fmt}, class={cls}"], None

    if fmt == "cog":
        fv = fc.get("cog_validate") or {}
        if fv.get("valid") is True:
            adj += 4
            evid.append("rio-cogeo: valid COG")
        elif fv.get("valid") is False:
            adj -= 12
            evid.append(f"rio-cogeo errors: {fv.get('errors')}")
            rem = ("gdal_translate in.tif out.tif -of COG -co COMPRESS=DEFLATE "
                   "-co BLOCKSIZE=512")
        overviews = fc.get("overviews")
        if overviews == []:
            adj -= 4
            evid.append("no overviews")
            rem = rem or "rio cogeo create in.tif out.tif (adds overviews)"
        codec0 = str((inv[0].get("codec") if inv else None) or "").lower()
        if codec0 in ("", "none"):
            adj -= 2
            evid.append("uncompressed")
        if any(x in codec0 for x in ("jpeg", "webp")):
            evid.append(f"lossy compression ({codec0}) - OK only for visualization products")
    elif fmt in ("zarr", "icechunk", "kerchunk"):
        consolidated = fc.get("consolidated")
        if consolidated:
            adj += 4
            evid.append(f"consolidated metadata (zarr_version={fc.get('zarr_version')})")
        elif consolidated is False:
            adj -= 4
            evid.append("no consolidated metadata")
            rem = "zarr.consolidate_metadata(store)"
        if fmt == "kerchunk":
            evid.append("virtual Zarr over archival files: access fixed, "
                        "underlying chunk layout frozen")
    elif fmt in ("hdf5", "netcdf4"):
        dsets = inv
        chunked = [d for d in dsets if d.get("chunks")]
        contiguous = [d.get("name") for d in dsets
                      if not d.get("chunks") and math.prod(d.get("shape") or [1]) > 1024]
        if dsets and not chunked:
            adj -= 6
            evid.append(f"contiguous datasets only: {contiguous[:5]}")
            rem = "h5repack -l CHUNK=... in.h5 out.h5 (see hdf5-netcdf.md)"
        else:
            tiny = []
            for d in chunked:
                nbytes = d.get("size_bytes")
                if nbytes is not None and d.get("shape") and d.get("chunks"):
                    total_elems = math.prod(d["shape"]) or 1
                    chunk_elems = math.prod(d["chunks"])
                    chunk_bytes = nbytes / total_elems * chunk_elems if total_elems else None
                    if chunk_bytes is not None and chunk_bytes < 1_000_000:
                        tiny.append(d.get("name"))
            if tiny:
                adj -= 4
                evid.append(f"chunks < 1 MB in: {tiny[:5]} (classic failure)")
                rem = "h5repack -l /var:CHUNK=1x512x512 in.h5 out.h5"
            elif chunked:
                adj += 4
                evid.append("chunked with >=1 MB chunks")
    elif fmt in CLOUD_HOSTILE:
        rem = _MIGRATIONS.get(fmt, "MANDATORY migration - see references/legacy.md")
        evid.append("cloud-hostile container: subsetting over HTTP is structurally "
                    "inefficient regardless of hosting")

    a_score = max(0.0, min(30.0, base + adj))
    status = "pass" if a_score >= 22 else ("partial" if a_score >= 10 else "fail")
    return C("A-class", "A", 30, a_score, status, "; ".join(evid), rem)


# ------------------------------------------------------------ B: metadata locality

def _b1_open_check(asset, fmt):
    open_ = asset.get("open") or {}
    tel = open_.get("telemetry") or {}
    fc = open_.get("format_checks") or {}
    mw = open_.get("metadata_walk")

    r2o = tel.get("requests_to_open")
    if isinstance(r2o, int) and not isinstance(r2o, bool):
        if r2o <= 3:
            pts, status = 8, "pass"
        elif r2o <= 10:
            pts, status = 5, "partial"
        else:
            pts, status = 1, "fail"
        evid = f"requests_to_open={r2o}, bytes_to_open={tel.get('bytes_to_open')}"
        if mw:
            evid += (f"; metadata_walk objects_visited={mw.get('objects_visited')}, "
                     f"complete={mw.get('complete')}")
    elif fmt in ("cog", "parquet", "pmtiles", "flatgeobuf", "copc"):
        pts, status = 7, "pass"
        evid = "static inference: header/footer/index front-loaded"
    elif fmt == "zarr" and fc.get("consolidated"):
        pts, status = 8, "pass"
        evid = "consolidated metadata => 1-2 requests"
    elif fmt in CLOUD_HOSTILE:
        pts, status = 1, "fail"
        evid = "no bounded-open path (sidecars/scan-based format)"
    else:
        pts, status = 4, "skipped"
        evid = "not measured (network/library unavailable); static value used"

    rem = None if status == "pass" else (
        "front-load/consolidate metadata (format-specific commands in references/)")
    return C("B1-open", "B", 8, pts, status, evid, rem)


def _b2_crs_check(asset, smoke_status):
    open_ = asset.get("open") or {}
    fc = open_.get("format_checks") or {}
    crs = fc.get("crs")
    crs_containers = fc.get("crs_containers") or []
    cf = ((asset.get("conventions") or {}).get("cf")) or {}
    grid_mapping_ok = next((c.get("ok") for c in (cf.get("checks") or [])
                            if c.get("id") == "grid_mapping"), None)
    net = smoke_status in ("pass", "fail")

    if crs or crs_containers or grid_mapping_ok is True:
        if crs:
            evid = f"crs={crs}"
        elif crs_containers:
            evid = f"crs_containers={[c.get('name') for c in crs_containers]}"
        else:
            evid = "grid_mapping variable resolved"
        return C("B2-crs", "B", 3, 3, "pass", evid)

    awarded = 0 if net else 1.5
    status = "fail" if net else "skipped"
    return C("B2-crs", "B", 3, awarded, status, "no machine-readable CRS found",
             "embed CRS in-file (or declare a CF grid_mapping variable)")


def _b3_semantics_check(asset):
    cf = ((asset.get("conventions") or {}).get("cf")) or {}
    cf_status = cf.get("status")
    pts_by_status = {"pass": (5, "pass"), "warn": (3, "partial"),
                     "fail": (1, "fail"), "skipped": (2.5, "skipped")}
    pts, status = pts_by_status.get(cf_status, (2.5, "skipped"))

    checks_list = cf.get("checks") or []
    notes = cf.get("notes") or []
    evid = ("; ".join(f"{c.get('id')}={c.get('ok')}" for c in checks_list)
            or "; ".join(notes) or "CF conventions not evaluated")
    rem = None if status == "pass" else (
        "declare CF attrs, units, nodata/_FillValue, scale/offset, grid_mapping")
    return C("B3-semantics", "B", 5, pts, status, evid, rem)


def _b4_catalog_check(asset, fmt, cf_status):
    open_ = asset.get("open") or {}
    inv = asset.get("inventory")
    if inv is None:
        inv = open_.get("inventory") or []

    pts, parts = 0.0, []
    if fmt and fmt != "unknown":
        pts += 1.5
        parts.append(f"format identified ({fmt})")
    if cf_status in ("pass", "warn"):
        pts += 1.5
        parts.append("self-describing attrs present (CF-shaped)")
    if any(v.get("attrs") for v in inv):
        pts += 1.0
        parts.append("variable-level attrs present in file")
    pts = min(4.0, pts)

    if pts == 0.0:
        return C("B4-catalog", "B", 4, 0.0, "n/a",
                 "no file-attrs evidence available to assess catalog quality (no "
                 "identified format, no CF signal, no variable attrs)")

    status = "pass" if pts >= 3 else "partial"
    rem = None if status == "pass" else (
        "add machine-readable media type/roles and richer variable attrs "
        "(units/standard_name) at the file level")
    return C("B4-catalog", "B", 4, pts, status, "; ".join(parts), rem)


# ------------------------------------------------------- C: chunking & AI fit

def _profile_statuses(variables, profile):
    out = []
    for v in variables:
        s = (v.get("profiles") or {}).get(profile, {}).get("status")
        if s and s != "unknown":
            out.append(s)
    return out


def _c1_interactive_check(asset, fmt, variables):
    statuses = _profile_statuses(variables, "interactive")
    open_ = asset.get("open") or {}
    fc = open_.get("format_checks") or {}

    if not statuses:
        pts, status = 2.5, "skipped"
        evid = "no measurable interactive chunk-size data"
    elif all(s == "within" for s in statuses):
        pts, status = 5.0, "pass"
        evid = f"interactive fit within target for all {len(statuses)} measured variable(s)"
    elif any(s == "within" for s in statuses):
        pts, status = 2.0, "partial"
        evid = f"interactive fit mixed across variables: {statuses[:5]}"
    else:
        pts, status = 0.0, "fail"
        evid = f"interactive fit outside target for all measured variable(s): {statuses[:5]}"

    if fmt == "cog" and fc.get("overviews") == []:
        pts, status = 0.0, "fail"
        evid += "; overviews missing (mandatory for interactive use)"

    rem = None if status == "pass" else "target ~1-4 MB tiles/chunks + overviews"
    return C("C1-interactive", "C", 5, pts, status, evid, rem)


def _c2_training_check(variables):
    statuses = _profile_statuses(variables, "training")
    if not statuses:
        pts, status = 3.5, "skipped"
        evid = "no measurable training chunk-size data"
    elif all(s == "within" for s in statuses):
        pts, status = 7.0, "pass"
        evid = f"training fit within target for all {len(statuses)} measured variable(s)"
    elif any(s == "within" for s in statuses):
        pts, status = 2.8, "partial"
        evid = f"training fit mixed across variables: {statuses[:5]}"
    else:
        pts, status = 0.0, "fail"
        evid = f"training fit outside target for all measured variable(s): {statuses[:5]}"
    rem = None if status == "pass" else (
        "rechunk to 32-64 MB chunks/shards aligned with the sampling pattern "
        "(rechunker recipe in references/zarr.md)")
    return C("C2-training", "C", 7, pts, status, evid, rem)


def _c3_agentic_check(variables, b1_status):
    statuses = _profile_statuses(variables, "agentic")
    if not statuses:
        return C("C3-agentic", "C", 7, 3.5, "skipped",
                 f"no measurable agentic chunk-size data; B1-open={b1_status}",
                 "consolidate metadata; stable HTTPS URLs; 1-16 MB chunks")

    fit_ok = all(s == "within" for s in statuses)
    evid = (f"agentic chunk fit={'within target' if fit_ok else statuses[:5]}; "
            f"B1-open={b1_status} (schema-enumerable-in-one-request "
            f"requirement per rubric.md C3)")

    if fit_ok and b1_status == "pass":
        pts, status = 7.0, "pass"
    elif fit_ok and b1_status == "partial":
        pts, status = 4.2, "partial"
    elif not fit_ok and b1_status == "pass":
        pts, status = 2.8, "partial"
    else:
        pts, status = 1.0, "fail"

    rem = None if status == "pass" else (
        "consolidate metadata; stable HTTPS URLs; 1-16 MB chunks")
    return C("C3-agentic", "C", 7, pts, status, evid, rem)


def _c4_count_check(variables):
    total = 0
    any_known = False
    for v in variables:
        shape, chunks = v.get("shape"), v.get("chunks")
        if shape and chunks:
            any_known = True
            try:
                total += math.prod(math.ceil(s / c) for s, c in zip(shape, chunks))
            except ZeroDivisionError:
                pass

    if not any_known:
        return C("C4-count", "C", 3, 1.5, "skipped",
                 "chunk grid not available", None)

    if total < 1_000_000:
        return C("C4-count", "C", 3, 3.0, "pass",
                 f"estimated total chunks ~= {total}")
    return C("C4-count", "C", 3, 0.0, "fail",
             f"estimated total chunks ~= {total} (>= 1,000,000)",
             "adopt Zarr v3 sharding (64-256 MB shards)")


def _c5_codec_check(compression):
    inspection = (compression or {}).get("inspection") or []
    if not inspection:
        return C("C5-codec", "C", 3, 1.5, "skipped", "codec not observed")

    statuses = [r.get("status") for r in inspection]
    codecs = sorted({r.get("codec") for r in inspection if r.get("codec")})

    if all(s == "pass" for s in statuses):
        return C("C5-codec", "C", 3, 3.0, "pass", f"codec(s)={codecs or ['modern']}")

    rem = next((r.get("remediation") for r in inspection if r.get("remediation")),
               "recompress with zstd level 3 + shuffle")
    if all(s == "fail" for s in statuses):
        return C("C5-codec", "C", 3, 0.0, "fail",
                 "no compression observed for measured variable(s)", rem)

    sample_ev = [(r.get("name"), r.get("codec")) for r in inspection][:5]
    return C("C5-codec", "C", 3, 1.8, "partial",
             f"mixed/weak codec(s): {sample_ev}", rem)


# ------------------------------------------------------- D: access & transport

_D_IDS_PTS = (("D1-range", 6), ("D2-auth", 3), ("D3-https-cors", 3), ("D4-cleanpath", 3))


def _d_checks(asset):
    findings = asset.get("access_findings") or []
    by_id = {}
    d0 = None
    for f in findings:
        fid = f.get("id") or ""
        if fid.startswith("D0-"):
            d0 = d0 or f
        else:
            by_id.setdefault(fid, f)

    default_reason = (f"not live-verified ({d0.get('evidence')})" if d0
                       else "not live-verified (no access findings provided)")

    out = []
    for cid, pts in _D_IDS_PTS:
        f = by_id.get(cid)
        if f is None:
            out.append(C(cid, "D", pts, pts * 0.5, "skipped", default_reason))
            continue
        status = f.get("status")
        status = "partial" if status == "warn" else status
        if status == "pass":
            awarded = pts
        elif status == "partial":
            awarded = pts * 0.5
        elif status == "skipped":
            awarded = pts * 0.5
        else:
            status = "fail"
            awarded = 0.0
        rem = f.get("remediation")
        out.append(C(cid, "D", pts, awarded, status,
                     f.get("evidence") or default_reason, rem))
    return out


# ------------------------------------------------------- E: reproducibility

def _e1_e2_checks(fmt, etag):
    is_icechunk = fmt == "icechunk"
    strong_etag = bool(etag) and not str(etag).startswith("W/")

    e1_awarded = 2.0 if is_icechunk else (1.0 if etag else 0.5)
    e1_status = "pass" if is_icechunk else ("partial" if etag else "fail")
    e1_rem = None if (is_icechunk or etag) else (
        "adopt versioning (Icechunk snapshots / stable ETags)")
    e1 = C("E1-version", "E", 2, e1_awarded, e1_status,
           f"etag={etag}, icechunk={is_icechunk}", e1_rem)

    e2_awarded = 2.0 if is_icechunk else (1.0 if strong_etag else 0.0)
    e2_status = "pass" if is_icechunk else ("partial" if strong_etag else "fail")
    e2_rem = None if (is_icechunk or strong_etag) else (
        "publish checksums (or ensure a strong ETag is served)")
    e2 = C("E2-checksums", "E", 2, e2_awarded, e2_status,
           f"strong_etag={strong_etag}, icechunk={is_icechunk}", e2_rem)
    return e1, e2


def _e_catalog_checks(cmr_meta):
    """E3-E7: CMR-umm-json-derived catalog evidence (module docstring).
    ``cmr_meta is None`` -> skipped (low confidence, not fail) for all
    five, per the task brief's explicit test case.
    """
    if cmr_meta is None:
        def skip(cid, pts, rem):
            return C(cid, "E", pts, pts * 0.5, "skipped",
                     "no CMR/catalog metadata available for this asset; not "
                     "verifiable from the file alone", rem)
        return [
            skip("E3-license", 2, "add a machine-readable license (or supply catalog metadata)"),
            skip("E4-citation", 1, "add DOI/citation metadata (or supply catalog metadata)"),
            skip("E5-contact", 1, "add maintainer contact (or supply catalog metadata)"),
            skip("E6-usage", 1, "publish a catalog entry linking runnable example "
                                "code / tutorial notebooks"),
            skip("E7-applications", 1, "describe common applications in catalog metadata"),
        ]

    lic = cmr_meta.get("license")
    e3 = C("E3-license", "E", 2, 2.0 if lic else 0.0, "pass" if lic else "fail",
           f"license={lic}", None if lic else "add a machine-readable license")

    cite = cmr_meta.get("doi") or cmr_meta.get("citation")
    e4 = C("E4-citation", "E", 1, 1.0 if cite else 0.0, "pass" if cite else "fail",
           f"citation={cite}" if cite else "catalog present but no DOI/citation",
           None if cite else "add DOI/citation (STAC scientific extension / cite-as)")

    contact = cmr_meta.get("contact")
    e5 = C("E5-contact", "E", 1, 1.0 if contact else 0.0, "pass" if contact else "fail",
           "providers/contacts declared in catalog" if contact
           else "catalog present but no providers/contacts",
           None if contact else "add provider/maintainer contact to catalog")

    doc_links = cmr_meta.get("doc_links")
    e6 = C("E6-usage", "E", 1, 1.0 if doc_links else 0.0, "pass" if doc_links else "fail",
           f"usage docs discoverable: {[link.get('href') for link in doc_links[:3]]}" if doc_links
           else "catalog has no example/tutorial/documentation links",
           None if doc_links else ("add rel=describedby/example links to sample code "
                                    "or notebooks in the collection"))

    n_kw = cmr_meta.get("n_keywords", 0) or 0
    desc_len = cmr_meta.get("description_len", 0) or 0
    if n_kw >= 3 or desc_len >= 200:
        e7 = C("E7-applications", "E", 1, 1.0, "pass",
               f"keywords={n_kw}, description_len={desc_len}")
    elif n_kw > 0 or desc_len > 0:
        e7 = C("E7-applications", "E", 1, 0.5, "partial",
               f"thin metadata: keywords={n_kw}, description_len={desc_len}",
               "expand keywords and description so agents can match the dataset "
               "to use cases")
    else:
        e7 = C("E7-applications", "E", 1, 0.0, "fail",
               "no keywords or description in catalog",
               "add keywords and a substantive description")

    return [e3, e4, e5, e6, e7]


# --------------------------------------------------------------- fail-remediation

def _ensure_fail_remediation(checks, fmt):
    """Rubric.md section 8: every fail/partial check must carry a
    remediation ("with an exact command where one exists"). This module
    constructs every check itself and normally always sets one for a
    fail (see each builder above), EXCEPT D-checks, which pass through
    an externally-supplied ``remediation`` from ``access_findings`` --
    if that caller left it empty, fill it in here (per-id fallback text,
    or the ported cloud-hostile migration dict for A-class) rather than
    letting a fail with no fix ship.
    """
    for c in checks:
        if c["status"] == "fail" and not c["remediation"]:
            if c["id"] == "A-class" and fmt in _MIGRATIONS:
                c["remediation"] = _MIGRATIONS[fmt]
            elif c["id"] in _D_FALLBACK_REMEDIATION:
                c["remediation"] = _D_FALLBACK_REMEDIATION[c["id"]]
            else:
                c["remediation"] = _GENERIC_FAIL_REMEDIATION
    return checks


# ------------------------------------------------------------------------ score

def score(asset: dict) -> dict:
    """Score one asset against the rubric.

    ``asset`` (every key optional/defensively read):

    - ``format`` (str|None): canonical format string (e.g. "zarr", "cog",
      "hdf5", "netcdf3", "hdf4", ...).
    - ``format_class`` (str|None): "cloud-native"|"cloud-optimizable"|
      "cloud-hostile"|"unknown"; derived from ``format`` via this
      module's own ``CLOUD_*`` tables if omitted.
    - ``access_findings`` (list[dict]): Task 4 D-dimension check-dicts,
      shape ``{"id", "dim", "status": "pass"|"warn"|"fail"|"skipped",
      "evidence", "remediation", "pts", "awarded"}`` (``pts``/``awarded``
      are ignored here -- this module assigns its own per rubric.md).
      ``id`` in ``{"D1-range", "D2-auth", "D3-https-cors",
      "D4-cleanpath"}`` is consumed directly; a ``"D0-*"`` finding (e.g.
      "D0-hosting"/"D0-network") supplies the default skip reason for
      any of the four that has no finding.
    - ``open`` (dict): an ``OpenResult``-shaped dict (Task 6's
      ``openers.open_dataset``): ``{"telemetry": {"requests_to_open",
      "bytes_to_open"}, "inventory": [...], "format_checks": {...},
      "metadata_walk": {...}|None}``.
    - ``inventory`` (list[dict]|None): Task 9's (possibly ranked/
      matched) variable inventory; falls back to ``open.inventory``.
    - ``chunking`` (dict): Task 7's ``assess_chunking()`` return,
      ``{"variables": [{"shape", "chunks", "profiles": {"interactive"|
      "training"|"agentic": {"status": "within"|"below"|"above"|
      "unknown"}}, ...}]}``.
    - ``compression`` (dict): Task 8's ``assess_compression()`` return,
      ``{"inspection": [{"name", "codec", "status", "remediation"}],
      ...}``.
    - ``conventions`` (dict): ``{"cf": Task 9 check_cf() return}``.
    - ``smoke_status`` (str): "pass"|"fail"|"skipped".
    - ``cmr_meta`` (dict|None): CMR-umm-json-derived catalog evidence,
      ``{"license", "doi"/"citation", "contact", "doc_links":
      [{"href"}], "n_keywords", "description_len"}``; ``None`` when no
      CMR granule ID was resolved.
    - ``etag`` (str|None, optional): HTTP ETag from a live HEAD probe,
      if the CLI captured one; not in the brief's asset shape, read
      defensively for E1/E2 only.
    - ``profile`` (str|None): "interactive"|"training"|"agentic";
      informational only here (chunking.py already used it to grade
      orientation amplification) -- every report still scores/prints
      all three C1/C2/C3 profiles per rubric.md section 4.

    Returns ``{"checks", "dimensions", "score", "tier", "verdict",
    "verdict_reason", "confidence", "caps_applied"}``.
    """
    fmt = asset.get("format")
    smoke_status = asset.get("smoke_status", "skipped")
    chunking = asset.get("chunking") or {}
    variables = chunking.get("variables") or []
    conventions = asset.get("conventions") or {}
    cf_status = (conventions.get("cf") or {}).get("status")

    checks = [_a_class_check(asset, fmt)]

    b1 = _b1_open_check(asset, fmt)
    checks.append(b1)
    checks.append(_b2_crs_check(asset, smoke_status))
    checks.append(_b3_semantics_check(asset))
    checks.append(_b4_catalog_check(asset, fmt, cf_status))

    checks.append(_c1_interactive_check(asset, fmt, variables))
    checks.append(_c2_training_check(variables))
    checks.append(_c3_agentic_check(variables, b1["status"]))
    checks.append(_c4_count_check(variables))
    checks.append(_c5_codec_check(asset.get("compression")))

    checks.extend(_d_checks(asset))

    e1, e2 = _e1_e2_checks(fmt, asset.get("etag"))
    checks.append(e1)
    checks.append(e2)
    checks.extend(_e_catalog_checks(asset.get("cmr_meta")))

    _ensure_fail_remediation(checks, fmt)

    dims, raw_score = rollup_checks(checks)

    caps_applied = []
    final_score = raw_score
    if smoke_status == "fail" and final_score > 74.0:
        final_score = 74.0
        caps_applied.append("smoke_fail_cap_74")
    consumer_fail = any(c["id"] in _CONSUMER_VISIBLE_FAIL_IDS and c["status"] == "fail"
                        for c in checks)
    if consumer_fail and final_score > 74.0:
        final_score = 74.0
        caps_applied.append("consumer_visible_fail_cap_74")

    tier = tier_for(final_score)
    verdict, reason = verdict_for(checks, smoke_status)

    n_skipped = sum(1 for c in checks if c["status"] == "skipped")
    if n_skipped == 0:
        confidence = "High"
    elif n_skipped <= 3:
        confidence = "Reduced"
    else:
        confidence = "Low"

    return {
        "checks": checks,
        "dimensions": dims,
        "score": final_score,
        "tier": tier,
        "verdict": verdict,
        "verdict_reason": reason,
        "confidence": confidence,
        "caps_applied": caps_applied,
    }
