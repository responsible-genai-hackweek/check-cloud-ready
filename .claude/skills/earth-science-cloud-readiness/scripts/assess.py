#!/usr/bin/env python3
"""Cloud-readiness assessment entry point.

Detects the input type (STAC API, static STAC, direct asset URL/prefix,
local file/dir, Croissant/GeoCroissant JSON-LD), crawls and samples assets,
dispatches per-format assessors, runs bounded smoke tests, scores against
the rubric (references/rubric.md), and writes findings.json.

Usage:
    python assess.py <INPUT> --workdir ./assessment \
        [--max-assets N] [--all] [--emit-croissant] [--no-network] \
        [--profile interactive|training|agentic]

Then render the report:
    python report.py ./assessment/findings.json -o assessment-report.md
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import smoke_test as st  # noqa: E402
import resolve_granule  # noqa: E402


def _try(name):
    try:
        return __import__(name)
    except Exception:
        return None

httpx = _try("httpx")
pystac = _try("pystac")
pystac_client = _try("pystac_client")
fsspec = _try("fsspec")

SKILL_DIR = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------- format tables
EXT_FORMATS = {
    ".tif": "cog", ".tiff": "cog", ".cog": "cog",
    ".zarr": "zarr",
    ".h5": "hdf5", ".hdf5": "hdf5", ".he5": "hdf5",
    ".hdf": "hdf4", ".hdf4": "hdf4", ".he2": "hdf4",
    ".nc": "netcdf-unknown", ".nc4": "hdf5", ".cdf": "netcdf-unknown",
    ".parquet": "parquet", ".geoparquet": "parquet",
    ".fgb": "flatgeobuf",
    ".laz": "las", ".las": "las", ".copc.laz": "copc",
    ".pmtiles": "pmtiles",
    ".grib": "grib2", ".grib2": "grib2", ".grb": "grib2", ".grb2": "grib2",
    ".shp": "shapefile", ".csv": "csv", ".txt": "csv",
    ".zip": "zip", ".tar": "tar", ".gz": "gzip", ".tgz": "tar",
    ".json": "json", ".jsonld": "json",
}

MEDIA_FORMATS = {
    "image/tiff; application=geotiff; profile=cloud-optimized": "cog",
    "image/tiff; application=geotiff": "cog",
    "image/tiff": "cog",
    "application/vnd+zarr": "zarr", "application/x-zarr": "zarr",
    "application/x-hdf5": "hdf5", "application/x-hdf": "hdf4",
    "application/netcdf": "netcdf-unknown", "application/x-netcdf": "netcdf-unknown",
    "application/x-parquet": "parquet", "application/vnd.apache.parquet": "parquet",
    "application/flatgeobuf": "flatgeobuf",
    "application/vnd.laszip+copc": "copc",
    "application/vnd.pmtiles": "pmtiles",
    "application/wmo-grib2": "grib2", "application/grib": "grib2",
    "application/zip": "zip", "text/csv": "csv",
}

CLOUD_NATIVE = {"cog", "zarr", "icechunk", "parquet", "copc", "flatgeobuf", "pmtiles"}
CLOUD_OPTIMIZABLE = {"hdf5", "netcdf4", "las", "kerchunk"}
CLOUD_HOSTILE = {"netcdf3", "grib2", "shapefile", "csv", "zip", "tar", "gzip", "hdf4"}

TIERS = [(90, "A", "Cloud-Native"), (75, "B", "Cloud-Optimized"),
         (55, "C", "Cloud-Friendly with gaps"), (35, "D", "Cloud-Hosted only"),
         (0, "F", "Not cloud-ready")]


def tier_for(score):
    for cut, letter, label in TIERS:
        if score >= cut:
            return letter, label
    return "F", "Not cloud-ready"


def magic_to_format(b: bytes) -> str | None:
    if not b:
        return None
    if b[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "cog"  # tiff family; COG-ness verified later
    if b[:8] == b"\x89HDF\r\n\x1a\n":
        return "hdf5"
    if b[:4] == b"\x0e\x03\x13\x01":
        return "hdf4"
    if b[:3] == b"CDF":
        return "netcdf3"
    if b[:4] == b"GRIB":
        return "grib2"
    if b[:4] == b"PAR1":
        return "parquet"
    if b[:3] == b"fgb":
        return "flatgeobuf"
    if b[:4] == b"LASF":
        return "las"
    if b[:7] == b"PMTiles":
        return "pmtiles"
    if b[:4] == b"PK\x03\x04":
        return "zip"
    if b[:2] == b"\x1f\x8b":
        return "gzip"
    if len(b) > 262 and b[257:262] == b"ustar":
        return "tar"
    if b[:4] == b"\x00\x00\x27\x0a":  # 9994 big-endian
        return "shapefile"
    return None


# ------------------------------------------------------------ input detection
CMR_GRANULE_RE = re.compile(r"^G\d+-[A-Z0-9_]+$")


def detect_input(raw: str) -> str:
    if CMR_GRANULE_RE.match(raw.strip()):
        return "cmr-granule"
    p = Path(raw)
    if p.exists():
        if p.is_dir():
            if (p / "zarr.json").exists() or (p / ".zmetadata").exists() or (p / ".zgroup").exists():
                return "asset"  # local zarr store
            if (p / "catalog.json").exists():
                return "stac_static"
            return "local_dir"
        if p.suffix.lower() in (".json", ".jsonld", ".geojson"):
            try:
                doc = json.loads(p.read_text())
                return _classify_json(doc)
            except Exception:
                return "asset"
        return "asset"
    u = urlparse(raw)
    if u.scheme in ("s3", "gs", "az"):
        return "asset"
    if u.scheme in ("http", "https"):
        low = raw.lower()
        if re.search(r"/stac(/v[0-9.]+)?/?$", low) or low.rstrip("/").endswith("/search"):
            return "stac_api"
        if re.search(r"/collections/[^/]+(/items[^/]*)?/?$", low):
            return "stac_static"  # single API collection or items endpoint: walk it
        if low.endswith((".json", ".jsonld")):
            doc = _fetch_json(raw)
            if doc is not None:
                return _classify_json(doc)
            return "stac_static"  # best guess; handled gracefully
        return "asset"
    raise ValueError(f"Cannot interpret input: {raw!r}")


def _classify_json(doc: dict) -> str:
    ctx = json.dumps(doc.get("@context", ""))
    if "mlcommons.org/croissant" in ctx or "geocroissant" in ctx.lower():
        return "croissant"
    t = doc.get("type") or doc.get("stac_version") and "Catalog"
    if doc.get("type") in ("Catalog", "Collection", "Feature") or "stac_version" in doc:
        if doc.get("conformsTo"):
            return "stac_api"
        return "stac_static"
    if "refs" in doc and "version" in doc:
        return "asset"  # kerchunk reference file
    return "stac_static"


def _fetch_json(url_or_path):
    p = Path(url_or_path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    if httpx is None:
        return None
    try:
        r = httpx.get(url_or_path, follow_redirects=True, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# --------------------------------------------------------------- asset gather
def gather_assets(raw, input_type, max_assets, sample_all, no_network, notes):
    """Return (assets, sample_frame). Each asset: dict(id,url,collection,
    media_type,format_hint,roles,item_datetime)."""
    if input_type == "asset":
        return ([{"id": Path(urlparse(raw).path or raw).name or raw, "url": raw,
                  "collection": None, "media_type": None, "format_hint": None,
                  "roles": [], "item_datetime": None}],
                {"strategy": "single asset", "selection_rule": "as given"})

    if input_type == "local_dir":
        files = [f for f in sorted(Path(raw).rglob("*")) if f.is_file()][:max_assets or 50]
        assets = [{"id": str(f.relative_to(raw)), "url": str(f), "collection": None,
                   "media_type": None, "format_hint": None, "roles": [],
                   "item_datetime": None} for f in files]
        return assets, {"strategy": "local directory walk",
                        "selection_rule": f"first {len(assets)} files (sorted)"}

    if input_type == "cmr-granule":
        return gather_cmr_granule(raw, max_assets, notes)

    if input_type == "croissant":
        return gather_croissant(raw, max_assets, notes)

    if input_type == "stac_api":
        return gather_stac_api(raw, max_assets, sample_all, no_network, notes)

    if input_type == "stac_static":
        return gather_stac_static(raw, max_assets, sample_all, notes)

    raise ValueError(input_type)


def _resolve_href(base_doc_url: str | None, href: str) -> str:
    """Resolve a possibly-relative asset href against the item document URL."""
    if not href or not base_doc_url or urlparse(href).scheme:
        return href
    if urlparse(base_doc_url).scheme in ("http", "https", "s3", "gs", "az"):
        from urllib.parse import urljoin
        return urljoin(base_doc_url, href)
    return str((Path(base_doc_url).parent / href).resolve())


def _stac_item_assets(item_dict, collection_id, base_doc_url=None):
    out = []
    dt = (item_dict.get("properties") or {}).get("datetime")
    for key, a in (item_dict.get("assets") or {}).items():
        roles = a.get("roles") or []
        if roles and not ({"data", "visual"} & set(roles)):
            continue  # skip pure thumbnails/metadata assets
        out.append({"id": f"{item_dict.get('id')}/{key}",
                    "url": _resolve_href(base_doc_url, a.get("href")),
                    "collection": collection_id, "media_type": a.get("type"),
                    "format_hint": None, "roles": roles, "item_datetime": dt,
                    "stac_item": {"id": item_dict.get("id"),
                                  "extensions": item_dict.get("stac_extensions", []),
                                  "properties_keys": sorted((item_dict.get("properties") or {}).keys()),
                                  "license": (item_dict.get("properties") or {}).get("license")}})
    return out


def _sample_items(items, n=3):
    """first, middle-ish, last by datetime."""
    items = sorted(items, key=lambda i: (i.get("properties") or {}).get("datetime") or "")
    if len(items) <= n:
        return items, "all items (count <= N)"
    picks = [items[0], items[len(items) // 2], items[-1]]
    return picks, "first, middle, last by item datetime"



DOC_RELS = {"about", "describedby", "documentation", "example", "tutorial",
            "handbook", "via", "cite-as"}
DOC_WORDS = ("example", "tutorial", "notebook", "sample", "how-to", "howto",
             "docs", "documentation", "quickstart", "user-guide", "userguide")


def _catalog_meta_from(doc: dict) -> dict:
    """Extract usage/applications discoverability signals from a STAC
    catalog/collection dict (or Croissant doc). Feeds E6/E7."""
    links = doc.get("links") or []
    doc_links = []
    for l in links:
        rel = str(l.get("rel", "")).lower()
        href = str(l.get("href", ""))
        title = str(l.get("title", "")).lower()
        if rel in DOC_RELS or any(w in href.lower() or w in title for w in DOC_WORDS):
            doc_links.append({"rel": rel, "href": href[:200]})
    desc = str(doc.get("description") or "")
    kw = doc.get("keywords") or []
    sci = bool(doc.get("sci:citation") or doc.get("sci:doi")
               or any(str(l.get("rel")) == "cite-as" for l in links))
    contact = bool(doc.get("providers") or doc.get("contacts")
                   or doc.get("creator") or doc.get("publisher"))
    return {"doc_links": doc_links[:8], "n_keywords": len(kw),
            "keywords_sample": [str(k) for k in kw[:8]],
            "description_len": len(desc), "sci_citation": sci,
            "contact": contact, "license": doc.get("license")}


def gather_stac_api(url, max_assets, sample_all, no_network, notes):
    assets, frames = [], []
    if pystac_client is None or no_network:
        notes.append("pystac-client unavailable or network disabled; STAC API crawl skipped")
        return assets, {"strategy": "stac_api", "error": "crawl skipped"}
    api_url = re.sub(r"/search/?$", "", url)
    cat = pystac_client.Client.open(api_url)
    for coll in cat.get_collections():
        try:
            search = cat.search(collections=[coll.id], max_items=200 if not sample_all else None)
            items = [i.to_dict() for i in search.items()]
        except Exception as e:
            notes.append(f"collection {coll.id}: search failed ({e})")
            continue
        picked, rule = (items, "all (--all)") if sample_all else _sample_items(items)
        try:
            cmeta = _catalog_meta_from(coll.to_dict())
        except Exception:
            cmeta = None
        # group by media type within the collection
        by_type = {}
        for it in picked:
            for a in _stac_item_assets(it, coll.id):
                a["catalog_meta"] = cmeta
                by_type.setdefault(a["media_type"], []).append(a)
        for mt, lst in by_type.items():
            assets.extend(lst[: (max_assets or 3)])
        frames.append({"collection": coll.id, "items_seen": len(items),
                       "rule": rule, "picked_items": [i.get("id") for i in picked]})
        if max_assets and len(assets) >= max_assets:
            assets = assets[:max_assets]
            break
    return assets, {"strategy": "STAC API, N=3 per collection (first/middle/last by "
                               "datetime), grouped by media type",
                    "collections": frames}


def gather_stac_static(url, max_assets, sample_all, notes):
    """Walk child/item links of a static catalog. No API assumptions."""
    assets, frames, seen = [], [], set()

    def walk(u, depth=0, cmeta=None):
        if u in seen or depth > 4 or (max_assets and len(assets) >= max_assets * 3):
            return
        seen.add(u)
        doc = _fetch_json(u)
        if doc is None:
            notes.append(f"could not fetch {u}")
            return
        base = u.rsplit("/", 1)[0]
        typ = doc.get("type")
        if typ == "Feature":  # an item
            frames.append({"item": doc.get("id"), "from": u})
            new = _stac_item_assets(doc, doc.get("collection"), base_doc_url=u)
            for a in new:
                a["catalog_meta"] = cmeta
            assets.extend(new)
            return
        if typ == "FeatureCollection":  # an /items endpoint page
            feats = doc.get("features") or []
            picked = feats if sample_all else (
                [feats[0], feats[len(feats) // 2], feats[-1]]
                if len(feats) > 3 else feats)
            frames.append({"items_page": u, "features_on_page": len(feats),
                           "note": "first page only; sample frame limited to it"})
            for it in picked:
                new = _stac_item_assets(it, it.get("collection"), base_doc_url=u)
                for a in new:
                    a["catalog_meta"] = cmeta
                assets.extend(new)
            return
        try:
            cmeta = _catalog_meta_from(doc)  # catalog/collection node
        except Exception:
            pass
        links = doc.get("links") or []
        items = [l for l in links if l.get("rel") == "item"]
        children = [l for l in links if l.get("rel") in ("child",)]
        # API-style collections expose an items *endpoint* (rel=items)
        children += [l for l in links if l.get("rel") == "items"]
        picked = items if sample_all else (
            [items[0], items[len(items) // 2], items[-1]] if len(items) > 3 else items)
        for l in picked + children:
            href = l.get("href") or ""
            walk(_resolve_href(u, href), depth + 1, cmeta=cmeta)

    walk(url)
    if max_assets:
        assets_out = assets[:max_assets]
    else:
        assets_out = assets
    return assets_out, {"strategy": "static STAC walk (child/item links), "
                                    "first/middle/last items per node",
                        "nodes_visited": len(seen), "trace": frames[:50]}


def gather_cmr_granule(granule_id, max_assets, notes):
    """Resolve a CMR granule concept ID to its S3 assets. The granule's own
    `/s3credentials` endpoint is carried on the sample frame so the run can
    read the (usually protected) NASA S3 objects directly — HTTPS URLs from
    CMR are recorded for reference only, never assessed in place of S3."""
    frame = {"strategy": "CMR granule resolution (UMM-G RelatedUrls)",
             "granule_id": granule_id, "selection_rule":
                 "every GET DATA VIA DIRECT ACCESS s3:// URL on the granule"}
    try:
        resolved = resolve_granule.resolve(granule_id)
    except SystemExit as e:  # resolve() is CLI-style: sys.exit(msg)
        notes.append(f"CMR resolution failed for granule {granule_id}: {e}")
        frame["error"] = str(e)
        return [], frame

    s3_urls = resolved.get("s3_urls") or []
    frame.update(credentials_url=resolved.get("credentials_url"),
                 provider=resolved.get("provider"),
                 s3_urls_found=len(s3_urls),
                 https_urls_found=len(resolved.get("https_urls") or []))
    if not s3_urls:
        notes.append(
            f"granule {granule_id} has no 'GET DATA VIA DIRECT ACCESS' s3:// "
            "URLs in CMR — nothing to assess in-region (HTTPS-only granules "
            "are not assessed as a substitute)")
        return [], frame
    if resolved.get("credentials_url") is None:
        notes.append(
            f"granule {granule_id} has no s3credentials endpoint in CMR; "
            "pass --credentials-url explicitly if the DAAC publishes one")

    assets = [{"id": Path(urlparse(u).path or u).name or u, "url": u,
               "collection": resolved.get("provider"), "media_type": None,
               "format_hint": None, "roles": ["data"], "item_datetime": None,
               "granule_id": granule_id}
              for u in s3_urls]
    if max_assets:
        assets = assets[:max_assets]
    return assets, frame


def gather_croissant(raw, max_assets, notes):
    doc = _fetch_json(raw)
    if doc is None:
        raise ValueError(f"cannot read Croissant document {raw}")
    assets, frame = [], {"strategy": "croissant distribution resolution",
                         "file_objects": [], "file_sets": []}
    dist = doc.get("distribution") or []
    objs = {d.get("@id") or d.get("name"): d for d in dist}
    geo_hints = {k: doc.get(k) for k in doc.keys()
                 if "boundingBox" in k or "spatial" in k.lower() or "crs" in k.lower()}
    cmeta = _catalog_meta_from({
        "links": [{"rel": "about", "href": u} for u in
                  ([doc.get("url")] if doc.get("url") else []) +
                  ([doc.get("sameAs")] if isinstance(doc.get("sameAs"), str)
                   else list(doc.get("sameAs") or []))],
        "description": doc.get("description"),
        "keywords": doc.get("keywords"),
        "sci:citation": doc.get("citeAs") or doc.get("citation"),
        "creator": doc.get("creator"), "publisher": doc.get("publisher"),
        "license": doc.get("license")})
    for d in dist:
        t = str(d.get("@type") or d.get("type") or "")
        name = d.get("name") or d.get("@id")
        if "FileObject" in t and d.get("contentUrl"):
            assets.append({"id": name, "url": d["contentUrl"], "collection": "croissant",
                           "media_type": d.get("encodingFormat"), "format_hint": None,
                           "roles": [], "item_datetime": None,
                           "catalog_meta": cmeta,
                           "croissant": {"sha256": d.get("sha256"),
                                         "contentSize": d.get("contentSize"),
                                         "geo_hints": geo_hints or None,
                                         "license": doc.get("license")}})
            frame["file_objects"].append(name)
        elif "FileSet" in t:
            frame["file_sets"].append({"name": name,
                                       "containedIn": d.get("containedIn"),
                                       "includes": d.get("includes"),
                                       "note": "resolve containedIn prefix/archive; "
                                               "archives are legacy red flags"})
            parent = d.get("containedIn")
            pid = parent.get("@id") if isinstance(parent, dict) else parent
            pobj = objs.get(pid)
            if pobj and pobj.get("contentUrl"):
                assets.append({"id": f"{name} (via {pid})", "url": pobj["contentUrl"],
                               "collection": "croissant", "media_type":
                               pobj.get("encodingFormat"), "format_hint": None,
                               "roles": [], "item_datetime": None,
                               "croissant": {"fileset": name}})
    if max_assets:
        assets = assets[:max_assets]
    return assets, frame


# ------------------------------------------------------------ format detection
def detect_format(asset, telemetry) -> str:
    # 1. extension
    path = urlparse(asset["url"]).path if "://" in asset["url"] else asset["url"]
    low = path.lower()
    if low.endswith(".copc.laz"):
        return "copc"
    fmt = None
    for ext, f in EXT_FORMATS.items():
        if low.endswith(ext):
            fmt = f
            break
    if low.rstrip("/").endswith(".zarr") or asset["url"].rstrip("/").endswith(".zarr"):
        fmt = "zarr"
    # 2. media type
    if fmt in (None, "json", "netcdf-unknown"):
        mt = (asset.get("media_type") or "").split(";")[0].strip()
        full = asset.get("media_type") or ""
        fmt2 = MEDIA_FORMATS.get(full) or MEDIA_FORMATS.get(mt)
        if fmt2:
            fmt = fmt2 if fmt in (None, "json") else fmt
    # 3. magic bytes: from smoke-test ranged read (remote) or local read
    hexed = (telemetry or {}).get("ranged", {}).get("first_bytes_hex")
    if not hexed and Path(asset["url"]).is_file():
        try:
            hexed = Path(asset["url"]).open("rb").read(300).hex()
        except Exception:
            hexed = None
    if hexed:
        m = magic_to_format(bytes.fromhex(hexed))
        if m:
            if fmt in (None, "json", "netcdf-unknown") or (fmt == "cog" and m != "cog"):
                fmt = m
            if fmt == "hdf4" and m == "hdf5":
                fmt = "hdf5"  # .hdf extension but HDF5 magic (common for NetCDF-4)
            if fmt == "netcdf-unknown":
                fmt = "hdf5" if m == "hdf5" else "netcdf3"
    if fmt == "netcdf-unknown":
        fmt = "hdf5"  # optimistic; check H5 magic during smoke test
    if fmt == "json":
        # kerchunk reference file?
        doc = _fetch_json(asset["url"])
        if doc and "refs" in doc:
            return "kerchunk"
        return "csv"  # generic sidecar-ish JSON data dump
    return fmt or "unknown"


# ------------------------------------------------------------------ scoring
def C(id_, dim, pts, awarded, status, evidence, remediation=None):
    return {"id": id_, "dimension": dim, "points_possible": pts,
            "points_awarded": round(awarded, 1), "status": status,
            "evidence": evidence, "remediation": remediation}


def score_asset(asset, fmt, tel):
    """Build checks per rubric.md; format specifics per references/*.md."""
    checks = []
    lo = tel.get("lazy_open", {}) if tel else {}
    head = tel.get("head", {}) if tel else {}
    ranged = tel.get("ranged", {}) if tel else {}
    net = tel.get("status") not in ("skipped", None)

    # ---------------- Dimension A: class baseline + format specifics
    if fmt in CLOUD_NATIVE:
        base, cls = 26, "cloud-native"
    elif fmt in CLOUD_OPTIMIZABLE:
        base, cls = 14, "cloud-optimizable"
    elif fmt in CLOUD_HOSTILE:
        base, cls = 4, "cloud-hostile"
    else:
        base, cls = 10, "unknown"
    adj, a_evid, a_rem = 0, [f"format={fmt}, class={cls}"], None

    if fmt == "cog":
        fv = tel.get("format_validation", {}) if tel else {}
        if fv.get("valid") is True:
            adj += 4; a_evid.append("rio-cogeo: valid COG")
        elif fv.get("valid") is False:
            adj -= 12; a_evid.append(f"rio-cogeo errors: {fv.get('errors')}")
            a_rem = ("gdal_translate in.tif out.tif -of COG -co COMPRESS=DEFLATE "
                     "-co BLOCKSIZE=512")
        ovr = lo.get("overviews")
        if ovr == [] and lo.get("status") == "pass":
            adj -= 4; a_evid.append("no overviews")
            a_rem = a_rem or "rio cogeo create in.tif out.tif (adds overviews)"
        comp = str(lo.get("compression") or "").lower()
        if "none" in comp and lo.get("status") == "pass":
            adj -= 2; a_evid.append("uncompressed")
        if any(x in comp for x in ("jpeg", "webp")):
            a_evid.append(f"lossy compression ({comp}) — OK only for visualization products")
    elif fmt in ("zarr", "icechunk", "kerchunk"):
        cm = lo.get("consolidated_metadata")
        if cm:
            adj += 4; a_evid.append(f"consolidated metadata: {cm.get('key')}")
        elif lo.get("status") == "pass":
            adj -= 4; a_evid.append("no consolidated metadata")
            a_rem = "zarr.consolidate_metadata(store)"
        if fmt == "kerchunk":
            a_evid.append("virtual Zarr over archival files: access fixed, "
                          "underlying chunk layout frozen")
    elif fmt in ("hdf5", "netcdf4"):
        dsets = lo.get("datasets") or {}
        chunked = [d for d in dsets.values() if d.get("layout") == "chunked"]
        contiguous = [n for n, d in dsets.items() if d.get("layout") == "contiguous"
                      and math.prod(d.get("shape") or [1]) > 1024]
        if dsets and not chunked:
            adj -= 6; a_evid.append(f"contiguous datasets only: {contiguous[:5]}")
            a_rem = "h5repack -l CHUNK=... in.h5 out.h5 (see hdf5-netcdf.md)"
        tiny = [n for n, d in dsets.items()
                if d.get("chunk_bytes_uncompressed") and d["chunk_bytes_uncompressed"] < 1_000_000]
        if tiny:
            adj -= 4; a_evid.append(f"chunks < 1 MB in: {tiny[:5]} (classic failure)")
            a_rem = "h5repack -l /var:CHUNK=1x512x512 in.h5 out.h5"
        else:
            if chunked:
                adj += 4; a_evid.append("chunked with >=1 MB chunks")
        fsp = (lo.get("file_space") or {})
        if "PAGE" in str(fsp.get("strategy", "")).upper():
            ps = fsp.get("page_size")
            if ps and 2 * 2**20 <= ps <= 16 * 2**20:
                adj += 5; a_evid.append(f"paged aggregation, page={ps}B (in 2-16MB band)")
            else:
                adj += 2; a_evid.append(f"paged aggregation, page={ps}B (outside 2-16MB)")
        elif lo.get("status") == "pass":
            a_evid.append("no paged aggregation detected")
            a_rem = a_rem or "h5repack -S PAGE -G 8388608 in.h5 out.h5"
    elif fmt in CLOUD_HOSTILE:
        mig = {
            "netcdf3": "MANDATORY migration: `nccopy -k nc4 -d 4 -c time/1,lat/512,"
                       "lon/512 in.nc out.nc4` or xarray→`to_zarr(zarr_format=3, "
                       "consolidated=True)` with chunks from chunking-for-ai.md; "
                       "interim: kerchunk virtual Zarr",
            "grib2": "MANDATORY migration: publish kerchunk refs "
                     "(`kerchunk.grib2.scan_grib`) now; convert via cfgrib→rechunk→"
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
                    "field out.tif`; multi-file collections → consolidated Zarr "
                    "via xarray (pyhdf/rioxarray reader → to_zarr)",
            "zip": "MANDATORY migration: unpack and publish members natively on "
                   "object storage; never leave data only inside archives",
        }
        mig["tar"] = mig["gzip"] = mig["zip"]
        a_rem = mig.get(fmt, "MANDATORY migration — see references/legacy.md")
        a_evid.append("cloud-hostile container: subsetting over HTTP is structurally "
                      "inefficient regardless of hosting")
    a_score = max(0.0, min(30.0, base + adj))
    checks.append(C("A-class", "A", 30, a_score,
                    "pass" if a_score >= 22 else ("partial" if a_score >= 10 else "fail"),
                    "; ".join(a_evid), a_rem))

    # ---------------- Dimension B
    r2o = lo.get("requests_to_open")
    if isinstance(r2o, int):
        if r2o <= 3:
            b1, b1s = 8, "pass"
        elif r2o <= 10:
            b1, b1s = 5, "partial"
        else:
            b1, b1s = 1, "fail"
        b1e = f"requests_to_open={r2o}, bytes_to_open={lo.get('bytes_to_open')}"
    elif fmt in ("cog", "parquet", "pmtiles", "flatgeobuf", "copc"):
        b1, b1s, b1e = 7, "pass", "static inference: header/footer/index front-loaded"
    elif fmt in ("zarr",) and lo.get("consolidated_metadata"):
        b1, b1s, b1e = 8, "pass", "consolidated metadata => 1-2 requests"
    elif fmt in CLOUD_HOSTILE:
        b1, b1s, b1e = 1, "fail", "no bounded-open path (sidecars/scan-based format)"
    else:
        b1, b1s, b1e = 4, "skipped", "not measured (network/library unavailable); static value used"
    checks.append(C("B1-open", "B", 8, b1, b1s, b1e,
                    None if b1s == "pass" else "front-load/consolidate metadata "
                    "(format-specific commands in references/)"))
    crs = lo.get("crs") or (lo.get("attrs_sample", {}) or {}).get("crs")
    stac_ext = (asset.get("stac_item") or {}).get("extensions", [])
    has_proj = any("proj" in e for e in stac_ext)
    if crs or has_proj:
        checks.append(C("B2-crs", "B", 3, 3, "pass", f"crs={crs or 'via STAC proj:'}"))
    else:
        checks.append(C("B2-crs", "B", 3, 0 if net else 1.5,
                        "fail" if net else "skipped",
                        "no machine-readable CRS found",
                        "embed CRS in-file and add STAC proj: extension"))
    sem = []
    attrs = lo.get("attrs_sample") or {}
    if any(k.lower() in ("conventions",) and "cf" in str(v).lower()
           for k, v in attrs.items()):
        sem.append("CF conventions declared")
    if lo.get("nodata") is not None:
        sem.append("nodata declared")
    b3 = 5 if len(sem) >= 2 else (3 if sem else (1 if net else 2.5))
    checks.append(C("B3-semantics", "B", 5, b3,
                    "pass" if b3 >= 5 else ("partial" if b3 >= 3 else
                                            ("skipped" if not net else "fail")),
                    "; ".join(sem) or "units/nodata/CF not confirmed",
                    None if b3 >= 5 else "declare CF attrs, units, nodata/_FillValue, "
                                         "scale/offset"))
    si = asset.get("stac_item")
    if si:
        pts = 1 + (1.5 if asset.get("media_type") else 0) + \
              (1.5 if any(e for e in si.get("extensions", [])
                          if any(x in e for x in ("proj", "raster", "datacube"))) else 0)
        checks.append(C("B4-catalog", "B", 4, min(4, pts),
                        "pass" if pts >= 3 else "partial",
                        f"media_type={asset.get('media_type')}, "
                        f"extensions={si.get('extensions')}",
                        None if pts >= 3 else "add asset roles + proj:/raster: "
                                              "extensions and checksums to STAC items"))
    else:
        checks.append(C("B4-catalog", "B", 4, 0, "n/a", "no catalog in input"))

    # ---------------- Dimension C (see chunking-for-ai.md)
    chunk_mb = _estimate_chunk_mb(fmt, lo)
    ev = f"estimated compressed chunk ≈ {chunk_mb} MB" if chunk_mb else \
         "chunk size not measurable"
    def band(lo_, hi_, pts):
        if chunk_mb is None:
            return pts * 0.5, "skipped"
        return (pts, "pass") if lo_ <= chunk_mb <= hi_ else \
               (pts * 0.4, "partial") if lo_ * 0.3 <= chunk_mb <= hi_ * 3 else (0, "fail")
    c1p, c1s = band(1, 4, 5)
    if fmt == "cog" and lo.get("overviews") == []:
        c1p, c1s = 0, "fail"
        ev += "; overviews missing (mandatory for interactive)"
    checks.append(C("C1-interactive", "C", 5, c1p, c1s, ev,
                    None if c1s == "pass" else "target ~1-4 MB tiles/chunks + overviews"))
    c2p, c2s = band(10, 100, 7)
    checks.append(C("C2-training", "C", 7, c2p, c2s, ev,
                    None if c2s == "pass" else
                    "rechunk to 32-64 MB chunks/shards aligned with sampling pattern "
                    "(rechunker recipe in references/zarr.md)"))
    agentic_reqs = _agentic_requests(fmt, lo)
    c3p = 7 if agentic_reqs and agentic_reqs <= 3 else \
        (4 if agentic_reqs and agentic_reqs <= 6 else (3.5 if agentic_reqs is None else 1))
    checks.append(C("C3-agentic", "C", 7, c3p,
                    "pass" if c3p >= 7 else ("skipped" if agentic_reqs is None else
                                             ("partial" if c3p >= 4 else "fail")),
                    f"catalog→variables→one subset ≈ {agentic_reqs or 'unmeasured'} requests; "
                    f"{ev}",
                    None if c3p >= 7 else "consolidate metadata; stable HTTPS URLs; "
                                          "1-16 MB chunks"))
    n_chunks = _estimate_chunk_count(lo)
    c4p = 3 if (n_chunks or 0) < 1_000_000 else 0
    checks.append(C("C4-count", "C", 3, c4p if n_chunks is not None else 1.5,
                    ("pass" if c4p else "fail") if n_chunks is not None else "skipped",
                    f"estimated total chunks ≈ {n_chunks}",
                    None if c4p else "adopt Zarr v3 sharding (64-256 MB shards)"))
    codec = str(lo.get("compression") or "").lower() or _zarr_codec(lo)
    trials = (tel or {}).get("compression_trials") or []
    best = trials[0] if trials else None
    trial_ev = (f"; measured on one sampled chunk: best {best['codec']} "
                f"ratio {best['ratio']}x, decode {best['decompress_MBps']} MB/s "
                f"({len(trials)} grid cells trialed)") if best else ""
    modern = codec and any(c in codec for c in ("zstd", "lz4", "blosc",
                                                "deflate", "snappy", "lzw"))
    weak = codec and any(c in codec for c in ("gzip", "zlib", "deflate")) \
        and "zstd" not in codec
    if modern and not weak:
        checks.append(C("C5-codec", "C", 3, 3, "pass", f"codec={codec}{trial_ev}"))
    elif codec and weak and best and best["ratio"] >= 1.3:
        checks.append(C("C5-codec", "C", 3, 2, "partial",
                        f"codec={codec}: works, but zstd dominates gzip/zlib on "
                        f"every axis{trial_ev}",
                        f"recompress with zstd level 3 + shuffle (measured "
                        f"{best['ratio']}x on sampled chunk); for floats, "
                        f"consider bit rounding (xbitinfo) first — precision "
                        f"filters beat codec tuning (see references/compression.md)"))
    elif modern:
        checks.append(C("C5-codec", "C", 3, 3, "pass", f"codec={codec}{trial_ev}"))
    elif codec:
        checks.append(C("C5-codec", "C", 3, 1, "partial",
                        f"codec={codec} (decode-speed risk for read-heavy AI loads)"
                        f"{trial_ev}",
                        "recompress with zstd level 3 + shuffle"
                        + (f" (measured {best['ratio']}x)" if best else "")))
    elif best:
        checks.append(C("C5-codec", "C", 3,
                        1 if best["ratio"] >= 1.3 else 2,
                        "fail" if best["ratio"] >= 1.3 else "partial",
                        f"no compression observed{trial_ev}",
                        f"data is compressible: apply zstd level 3 + shuffle "
                        f"(measured {best['ratio']}x on sampled chunk)"
                        if best["ratio"] >= 1.3 else
                        "data near-incompressible; storing raw is defensible"))
    else:
        checks.append(C("C5-codec", "C", 3, 1.5, "skipped", "codec not observed"))

    # ---------------- Dimension D (live only)
    if not net or not head:
        reason = (tel.get("reason") if tel else None) or \
                 ("local file: transport not applicable/verifiable" if not head
                  else "no network")
        for cid, pts in (("D1-range", 6), ("D2-auth", 3), ("D3-https-cors", 3),
                         ("D4-cleanpath", 3)):
            checks.append(C(cid, "D", pts, pts * 0.5, "skipped",
                            f"not live-verified ({reason})"))
    else:
        rs = ranged.get("status")
        checks.append(C("D1-range", "D", 6, 6 if rs == "pass" else 0,
                        rs or "skipped",
                        json.dumps(ranged.get("reads", []))[:400],
                        None if rs == "pass" else "enable/verify HTTP Range support "
                        "(206 responses) on the data endpoint"))
        auth_ok = head.get("http_status") not in (401, 403)
        if head.get("transport") == "s3-authenticated":
            d2_evidence = ("authenticated S3 head OK"
                           if head.get("status") == "pass"
                           else f"authenticated S3 head status={head.get('status')}")
        else:
            d2_evidence = f"HTTP {head.get('http_status')}"
        checks.append(C("D2-auth", "D", 3, 3 if auth_ok else 1,
                        "pass" if auth_ok else "partial",
                        d2_evidence,
                        None if auth_ok else "document auth clearly; flag requester-pays"))
        tls = head.get("tls_ok")
        if head.get("transport") == "s3-authenticated":
            # SDK-transport S3: TLS is enforced by the SDK and CORS is not
            # applicable (no browser/XHR path involved), so the CORS aspect
            # gets full credit rather than being scored against the asset.
            checks.append(C("D3-https-cors", "D", 3, 3, "pass",
                            "S3 SDK transport; CORS n/a", None))
        else:
            cors = head.get("cors_allow_origin")
            d3 = (2 if tls else 0) + (1 if cors else 0)
            checks.append(C("D3-https-cors", "D", 3, d3,
                            "pass" if d3 == 3 else "partial",
                            f"tls={tls}, CORS Allow-Origin={cors!r}",
                            None if d3 == 3 else "serve over HTTPS; add CORS headers if "
                            "browser use is plausible"))
        redirects = head.get("redirects", 0)
        html = "text/html" in str(head.get("content_type", ""))
        d4 = 3 if (redirects <= 1 and not html) else (1 if not html else 0)
        checks.append(C("D4-cleanpath", "D", 3, d4,
                        "pass" if d4 == 3 else "fail",
                        f"redirects={redirects}, content_type={head.get('content_type')}",
                        None if d4 == 3 else "remove redirect chains / HTML "
                        "interstitials in front of data URLs"))

    # ---------------- Dimension E
    etag = head.get("etag")
    cro = asset.get("croissant") or {}
    cm = asset.get("catalog_meta")
    checks.append(C("E1-version", "E", 2,
                    2 if fmt == "icechunk" else (1 if etag else 0.5),
                    "pass" if fmt == "icechunk" else ("partial" if etag else "fail"),
                    f"etag={etag}, icechunk={fmt == 'icechunk'}",
                    None if etag else "adopt versioning (Icechunk snapshots / "
                    "STAC item versions)"))
    strong_etag = bool(etag) and not str(etag).startswith("W/")
    checks.append(C("E2-checksums", "E", 2,
                    2 if cro.get("sha256") else (1 if strong_etag else 0),
                    "pass" if cro.get("sha256") else ("partial" if strong_etag else "fail"),
                    f"sha256={'declared' if cro.get('sha256') else 'none'}, "
                    f"strong_etag={strong_etag}",
                    None if cro.get("sha256") or strong_etag else "publish checksums"))
    lic = (cro.get("license") or (asset.get("stac_item") or {}).get("license")
           or (cm or {}).get("license"))
    checks.append(C("E3-license", "E", 2, 2 if lic else 0,
                    "pass" if lic else "fail", f"license={lic}",
                    None if lic else "add a machine-readable license"))
    if cm and cm.get("sci_citation"):
        checks.append(C("E4-citation", "E", 1, 1, "pass",
                        "DOI/citation declared in catalog"))
    elif cm:
        checks.append(C("E4-citation", "E", 1, 0, "fail",
                        "catalog present but no sci:citation/sci:doi/cite-as",
                        "add DOI/citation (STAC scientific extension)"))
    else:
        checks.append(C("E4-citation", "E", 1, 0.5, "skipped",
                        "DOI/citation not verifiable from asset alone",
                        "add DOI/citation to catalog"))
    if cm and cm.get("contact"):
        checks.append(C("E5-contact", "E", 1, 1, "pass",
                        "providers/contacts declared in catalog"))
    elif cm:
        checks.append(C("E5-contact", "E", 1, 0, "fail",
                        "catalog present but no providers/contacts",
                        "add provider/maintainer contact to catalog"))
    else:
        checks.append(C("E5-contact", "E", 1, 0.5, "skipped",
                        "contact not verifiable from asset alone",
                        "add maintainer contact to catalog"))
    # E6: usage discoverability — could an agent find how to use this data
    # (sample code / tutorial / docs links) without guessing?
    if cm is None:
        checks.append(C("E6-usage", "E", 1, 0.5, "skipped",
                        "no catalog in input; an agent would have to research "
                        "or guess how to access this data",
                        "publish a catalog entry linking runnable example "
                        "code / tutorial notebooks"))
    elif cm.get("doc_links"):
        checks.append(C("E6-usage", "E", 1, 1, "pass",
                        f"usage docs discoverable: "
                        f"{[l['href'] for l in cm['doc_links'][:3]]}"))
    else:
        checks.append(C("E6-usage", "E", 1, 0, "fail",
                        "catalog has no example/tutorial/documentation links; "
                        "agents must guess access patterns",
                        "add rel=describedby/example links to sample code or "
                        "notebooks in the collection"))
    # E7: applications discoverability — can an agent match this dataset to a
    # use case (keywords / substantive description)?
    if cm is None:
        checks.append(C("E7-applications", "E", 1, 0.5, "skipped",
                        "no catalog in input; use cases not discoverable",
                        "describe common applications in catalog metadata"))
    elif cm.get("n_keywords", 0) >= 3 or cm.get("description_len", 0) >= 200:
        checks.append(C("E7-applications", "E", 1, 1, "pass",
                        f"keywords={cm.get('n_keywords')}, "
                        f"description_len={cm.get('description_len')}"))
    elif cm.get("n_keywords", 0) > 0 or cm.get("description_len", 0) > 0:
        checks.append(C("E7-applications", "E", 1, 0.5, "partial",
                        f"thin metadata: keywords={cm.get('n_keywords')}, "
                        f"description_len={cm.get('description_len')}",
                        "expand keywords and description so agents can match "
                        "the dataset to use cases"))
    else:
        checks.append(C("E7-applications", "E", 1, 0, "fail",
                        "no keywords or description in catalog",
                        "add keywords and a substantive description"))
    return checks


def _estimate_chunk_mb(fmt, lo):
    try:
        if fmt == "cog" and lo.get("blockshapes"):
            bh, bw = lo["blockshapes"][0]
            dt = lo.get("dtypes", ["uint8"])[0]
            size = {"uint8": 1, "int16": 2, "uint16": 2, "int32": 4, "float32": 4,
                    "float64": 8}.get(dt, 2)
            ratio = 1 if "none" in str(lo.get("compression", "")).lower() else 2
            return round(bh * bw * size / ratio / 1e6, 2)
        if lo.get("variables"):
            v = next(iter(lo["variables"].values()))
            ch, dt = v.get("chunks"), v.get("dtype", "float32")
            if ch:
                size = 8 if "64" in dt else (4 if "32" in dt else 2)
                return round(math.prod(ch) * size / 2 / 1e6, 2)  # assume 2:1
        if lo.get("datasets"):
            d = next(iter(lo["datasets"].values()))
            if d.get("chunk_bytes_uncompressed"):
                return round(d["chunk_bytes_uncompressed"] / 2 / 1e6, 2)
        if lo.get("row_group_bytes", {}).get("median"):
            return round(lo["row_group_bytes"]["median"] / 1e6, 2)
    except Exception:
        return None
    return None


def _estimate_chunk_count(lo):
    try:
        total = 0
        for v in (lo.get("variables") or {}).values():
            sh, ch = v.get("shape"), v.get("chunks")
            if sh and ch:
                total += math.prod(math.ceil(s / c) for s, c in zip(sh, ch))
        for d in (lo.get("datasets") or {}).values():
            sh, ch = d.get("shape"), d.get("chunks")
            if sh and ch:
                total += math.prod(math.ceil(s / c) for s, c in zip(sh, ch))
        if not total and lo.get("num_row_groups"):
            total = lo["num_row_groups"]
        return total or None
    except Exception:
        return None


def _zarr_codec(lo):
    return ""  # codec surfaced via compression key when available


def _agentic_requests(fmt, lo):
    r2o = lo.get("requests_to_open")
    if isinstance(r2o, int):
        return r2o + 1  # + one subset read
    if fmt in ("cog", "parquet", "pmtiles", "flatgeobuf", "copc"):
        return 2 if lo.get("status") == "pass" else 3
    if fmt in ("zarr",) and lo.get("consolidated_metadata"):
        return 2
    if fmt in CLOUD_HOSTILE:
        return 99
    return None


def rollup_checks(checks):
    dims = {}
    for d in "ABCDE":
        cs = [c for c in checks if c["dimension"] == d and c["status"] != "n/a"]
        poss = sum(c["points_possible"] for c in cs)
        got = sum(c["points_awarded"] for c in cs)
        full = {"A": 30, "B": 20, "C": 25, "D": 15, "E": 10}[d]
        dims[d] = {"score": round(got / poss * full, 1) if poss else 0.0, "max": full}
    return dims


# ---------------------------------------------------------- croissant emitter
def emit_croissant(findings, out_path):
    """Isolated GeoCroissant emitter. Reads findings.json structures ONLY.
    GeoCroissant is a stabilizing draft: keep churn here, never in scoring."""
    ru = findings["rollup"]
    dists, recordsets = [], []
    for a in findings["assets"]:
        head = (a.get("smoke_test") or {}).get("head", {})
        etag = str(head.get("etag") or "").strip('"')
        d = {"@type": "cr:FileObject", "@id": a["id"], "name": a["id"],
             "contentUrl": a["url"],
             "encodingFormat": a.get("media_type") or a.get("format")}
        if head.get("content_length"):
            d["contentSize"] = f"{head['content_length']} B"
        if etag and re.fullmatch(r"[0-9a-f]{32}", etag):
            d["md5"] = etag
        elif etag:
            d["description"] = f"ETag (not MD5, possibly multipart): {etag}"
        dists.append(d)
        varz = ((a.get("smoke_test") or {}).get("lazy_open") or {}).get("variables") or {}
        for vname, v in list(varz.items())[:10]:
            recordsets.append({
                "@type": "cr:RecordSet", "@id": f"{a['id']}/{vname}",
                "name": vname,
                "description": "auto-generated sketch from cloud-readiness assessment",
                "field": [{"@type": "cr:Field", "name": vname,
                           "dataType": v.get("dtype"),
                           "description": f"dims={v.get('dims')}, shape={v.get('shape')}"}]})
    doc = {
        "@context": {
            "@vocab": "https://schema.org/",
            "cr": "http://mlcommons.org/croissant/",
            "rai": "http://mlcommons.org/croissant/RAI/",
            "geocr": "http://mlcommons.org/croissant/geocroissant/",
            "cloudReadiness": "https://cloudnativegeo.org/ns/cloud-readiness#",
        },
        "@type": "sc:Dataset",
        "name": findings["input"]["raw"],
        "conformsTo": "http://mlcommons.org/croissant/1.0",
        "description": "GeoCroissant record emitted by earth-science-cloud-readiness "
                       "assessment (draft spec; emitter isolated from rubric engine).",
        "distribution": dists,
        "recordSet": recordsets,
        "cloudReadiness:tier": ru["dataset_tier"],
        "cloudReadiness:score": ru["dataset_score"],
        "cloudReadiness:confidence": ru["confidence"],
        "cloudReadiness:smokeTest": ru["smoke_test_overall"],
        "cloudReadiness:chunkProfileInteractive": ru.get("chunk_profiles", {}).get("interactive"),
        "cloudReadiness:chunkProfileTraining": ru.get("chunk_profiles", {}).get("training"),
        "cloudReadiness:chunkProfileAgentic": ru.get("chunk_profiles", {}).get("agentic"),
        "cloudReadiness:findings": "findings.json",
        "rai:dataCollection": f"Assessment sample frame: {json.dumps(findings['sample_frame'])[:800]}",
        "rai:dataLimitations": "Scores reflect sampled assets only; coverage gaps, if any, "
                               "are listed in the assessment report.",
    }
    lic = None
    for a in findings["assets"]:
        lic = (a.get("croissant") or {}).get("license") or lic
    if lic:
        doc["license"] = lic
    Path(out_path).write_text(json.dumps(doc, indent=2))
    return out_path


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="STAC API / catalog.json / asset URL / local path / "
                                  "Croissant JSON-LD")
    ap.add_argument("--workdir", default="./assessment")
    ap.add_argument("--max-assets", type=int, default=None)
    ap.add_argument("--all", action="store_true", dest="sample_all",
                    help="assess every item, not the N=3 sample")
    ap.add_argument("--emit-croissant", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--variables", default=None,
                    help="comma-separated variable/band/column names the user "
                         "cares about; steers the one-chunk read and chunk-shape "
                         "commentary")
    ap.add_argument("--profile", choices=["interactive", "training", "agentic"],
                    default=None, help="primary intended access profile, if known")
    ap.add_argument("--granule-id",
                    help="CMR granule concept ID (e.g. G4289749526-ASF) whose "
                         "s3credentials endpoint should be used for NASA "
                         "Earthdata S3 assets")
    ap.add_argument("--credentials-url",
                    help="DAAC s3credentials endpoint (wins over the endpoint "
                         "resolved from a granule ID)")
    ap.add_argument("--earthaccess-fallback", action="store_true",
                    help="opt-in earthaccess credential fallback (never used "
                         "implicitly)")
    args = ap.parse_args()
    var_list = [v.strip() for v in (args.variables or "").split(",") if v.strip()] or None

    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    notes = []
    input_type = detect_input(args.input)
    print(f"[assess] input type: {input_type}", file=sys.stderr)

    assets, frame = gather_assets(args.input, input_type, args.max_assets,
                                  args.sample_all, args.no_network, notes)

    # Credentials endpoint: explicit flag wins, then the one CMR published for
    # --granule-id, then the one the resolved input carried on its sample frame.
    granule_id = args.granule_id or (args.input if input_type == "cmr-granule"
                                     else None)
    credentials_url = args.credentials_url
    if not credentials_url and args.granule_id:
        credentials_url = st.resolve_credentials_url(granule_id=args.granule_id)
    if not credentials_url:
        credentials_url = (frame or {}).get("credentials_url")
    if credentials_url:
        print(f"[assess] NASA S3 credentials endpoint: {credentials_url}",
              file=sys.stderr)
    nasa_access = ("obstore-cmr" if credentials_url else
                   "earthaccess-fallback" if args.earthaccess_fallback else
                   "anon" if any(urlparse(a["url"]).scheme in ("s3", "gs", "az")
                                 for a in assets) else None)
    input_meta = {"raw": args.input, "type": input_type,
                  "profile_hint": args.profile,
                  "granule_id": granule_id,
                  "credentials_url": credentials_url}
    if isinstance(frame, dict):  # surfaced in the report's sample-frame block
        if granule_id:
            frame.setdefault("granule_id", granule_id)
        if credentials_url:
            frame.setdefault("credentials_url", credentials_url)
    if not assets:
        # Unassessable is not the same claim as F: F means "assessed and it
        # failed"; this means we could not reach/parse the input at all.
        fetch_fails = [n for n in notes if "could not fetch" in n or "skipped" in n]
        notes.append("UNASSESSABLE: no assets could be gathered from the input "
                     "(network egress blocked, auth required, or the input has "
                     "no reachable items). No tier or score is assigned.")
        findings = {"skill": "earth-science-cloud-readiness",
                    "generated": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "input": input_meta,
                    "sample_frame": frame,
                    "environment": {"network": not args.no_network,
                                    "nasa_access": nasa_access,
                                    "libraries": {m: (_try(m) is not None) for m in
                                                  ("rasterio", "rio_cogeo", "zarr",
                                                   "xarray", "h5py", "fsspec", "s3fs",
                                                   "pyarrow", "pystac", "pystac_client",
                                                   "httpx", "obstore")}},
                    "notes": notes, "assets": [],
                    "rollup": {"by_media_type": {}, "dominant_format": None,
                               "dataset_score": None, "dataset_tier": None,
                               "dataset_tier_label": "Unassessable",
                               "confidence": "none",
                               "smoke_test_overall": "skipped",
                               "unassessable_reasons": fetch_fails,
                               "chunk_profiles": {}}}
        out = Path(args.workdir); out.mkdir(parents=True, exist_ok=True)
        fp = out / "findings.json"
        fp.write_text(json.dumps(findings, indent=1))
        print(f"[assess] wrote {fp} (unassessable)", file=sys.stderr)
        print(json.dumps({"findings": str(fp), "tier": None, "score": None,
                          "confidence": "none", "status": "unassessable"}))
        return
    results = []
    for a in assets:
        fmt0 = detect_format(a, None)
        tel = st.run_smoke_test(a["url"], fmt0, no_network=args.no_network,
                                variables=var_list,
                                credentials_url=credentials_url,
                                earthaccess_fallback=args.earthaccess_fallback)
        fmt = detect_format(a, tel)
        if fmt != fmt0:
            tel = st.run_smoke_test(a["url"], fmt, no_network=args.no_network,
                                    variables=var_list,
                                    credentials_url=credentials_url,
                                    earthaccess_fallback=args.earthaccess_fallback)
        checks = score_asset(a, fmt, tel)
        dims = rollup_checks(checks)
        score = round(sum(d["score"] for d in dims.values()), 1)
        smoke_status = tel.get("status", "skipped")
        capped = False
        if smoke_status == "fail" and score > 74:
            score, capped = 74.0, True
        letter, label = tier_for(score)
        results.append({**a, "format": fmt, "checks": checks, "dimensions": dims,
                        "score": score, "tier": letter, "tier_label": label,
                        "score_capped_by_smoke_fail": capped, "smoke_test": tel})
        print(f"[assess] {a['id']}: {fmt} -> {score} ({letter}), "
              f"smoke={smoke_status}", file=sys.stderr)

    # rollup per media type / format
    by_fmt = {}
    for r in results:
        by_fmt.setdefault(r["format"], []).append(r["score"])
    fmt_roll = {f: {"count": len(v), "median_score": sorted(v)[len(v) // 2],
                    "tier": tier_for(sorted(v)[len(v) // 2])[0]}
                for f, v in by_fmt.items()}
    dominant = max(by_fmt, key=lambda f: len(by_fmt[f])) if by_fmt else None
    dataset_score = fmt_roll[dominant]["median_score"] if dominant else 0
    d_letter, d_label = tier_for(dataset_score)
    statuses = [r["smoke_test"].get("status") for r in results]
    smoke_overall = ("fail" if "fail" in statuses else
                     "skipped" if statuses and all(s == "skipped" for s in statuses) else
                     "pass" if statuses else "skipped")
    confidence = ("high" if smoke_overall == "pass"
                  else "reduced" if smoke_overall == "skipped" else "high")
    if smoke_overall == "skipped":
        notes.append("smoke test skipped (network/auth); static-only assessment — "
                     "confidence reduced, scores use static-inference values")

    def prof(r, cid):
        c = next((c for c in r["checks"] if c["id"] == cid), {})
        return f"{c.get('status')} ({c.get('evidence', '')[:80]})"
    ru = {"by_media_type": fmt_roll, "dominant_format": dominant,
          "dataset_score": dataset_score, "dataset_tier": d_letter,
          "dataset_tier_label": d_label, "confidence": confidence,
          "smoke_test_overall": smoke_overall,
          "chunk_profiles": ({"interactive": prof(results[0], "C1-interactive"),
                              "training": prof(results[0], "C2-training"),
                              "agentic": prof(results[0], "C3-agentic")}
                             if results else {})}

    findings = {
        "skill": "earth-science-cloud-readiness",
        "generated": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "input": input_meta,
        "sample_frame": frame,
        "environment": {"network": not args.no_network,
                        "nasa_access": nasa_access,
                        "libraries": {m: (_try(m) is not None) for m in
                                      ("rasterio", "rio_cogeo", "zarr", "xarray",
                                       "h5py", "fsspec", "s3fs", "pyarrow",
                                       "pystac", "pystac_client", "httpx",
                                       "obstore")}},
        "notes": notes,
        "assets": results,
        "rollup": ru,
    }
    out = work / "findings.json"
    out.write_text(json.dumps(findings, indent=2, default=str))
    print(f"[assess] wrote {out}", file=sys.stderr)

    if args.emit_croissant:
        cro = emit_croissant(findings, work / "croissant.jsonld")
        print(f"[assess] wrote {cro}", file=sys.stderr)

    print(json.dumps({"findings": str(out), "tier": d_letter,
                      "score": dataset_score, "confidence": confidence,
                      "smoke_test": smoke_overall}))


if __name__ == "__main__":
    main()
