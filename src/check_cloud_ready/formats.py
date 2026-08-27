"""Input classification and format detection.

Merges the sniffing logic from the check-cloud-ready skill's
``detect_format.py`` (magic-byte and store-layout detection) with the
extension/class hint tables from the earth-science-cloud-readiness
skill's ``assess.py``. STAC, Croissant, and catalog-crawling concerns
are intentionally dropped — this module is scoped to: given a single
input string or set of bytes, what format is it (or might it be)?

Format identifiers used throughout this module are short, lowercase
canonical strings (``"zarr"``, ``"hdf5"``, ``"netcdf4"``, ``"netcdf3"``,
``"hdf4"``, ``"cog"``, ``"grib2"``, ``"parquet"``, ``"kerchunk"``,
``"icechunk"``, ...) so that ``format_class`` can classify the output
of ``sniff_bytes``/``sniff_store``/``refine_hdf5``/``ext_hint``
uniformly.
"""
import json
import re
from urllib.parse import urlparse

# --------------------------------------------------------------- input class
GRANULE_ID_RE = re.compile(r"^G\d+-[A-Z0-9_.]+$")
URL_SCHEMES = {"http", "https", "s3", "gs", "az", "abfs"}


def detect_input(s: str) -> str:
    """Classify a raw input string as one of "granule-id", "url", or
    "local". No network access, no filesystem existence check is
    required for the "local" classification — an existing path or a
    bare path-like string without a URL scheme both count as "local".
    """
    if GRANULE_ID_RE.match(s):
        return "granule-id"
    scheme = urlparse(s).scheme
    if scheme in URL_SCHEMES:
        return "url"
    return "local"


# ------------------------------------------------------------- extension hint
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


def ext_hint(path_or_url: str) -> str | None:
    """Extension-table lookup: a cheap pre-network hint at format.
    Returns None if no extension in EXT_FORMATS matches.
    """
    path = urlparse(path_or_url).path if "://" in path_or_url else path_or_url
    low = path.lower()
    # Longest extension first so multi-part suffixes (e.g. ".copc.laz")
    # win over shorter ones (e.g. ".laz") that would otherwise match first.
    for ext in sorted(EXT_FORMATS, key=len, reverse=True):
        if low.endswith(ext):
            return EXT_FORMATS[ext]
    return None


# --------------------------------------------------------------- magic bytes
HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"
HDF5_OFFSETS = (0, 512, 1024, 2048, 4096)  # superblock may follow a user block

# (magic bytes, canonical format, optional variant note)
MAGIC = [
    (HDF5_MAGIC, "hdf5", None),
    (b"CDF\x01", "netcdf3", "netcdf-3 classic"),
    (b"CDF\x02", "netcdf3", "netcdf-3 64-bit offset"),
    (b"CDF\x05", "netcdf3", "netcdf-3 64-bit data (CDF-5)"),
    (b"\x0e\x03\x13\x01", "hdf4", None),
]


def sniff_bytes(head: bytes, *, offset_reads=None) -> dict:
    """Magic-byte detection over the first bytes of a file.

    ``offset_reads``, if given, is a callable ``(offset, size) -> bytes``
    used to probe additional offsets (e.g. the HDF5 superblock after a
    user block, or more of a JSON blob than fits in ``head``) so that
    remote/multi-offset probing stays possible without this function
    doing any I/O itself.
    """
    notes: list[str] = []
    out = {"format": None, "confidence": "magic", "notes": notes}

    for magic, fmt, variant in MAGIC:
        if head.startswith(magic):
            out["format"] = fmt
            if variant:
                notes.append(variant)
            return out

    if offset_reads is not None:
        for off in HDF5_OFFSETS[1:]:
            try:
                chunk = offset_reads(off, len(HDF5_MAGIC))
            except Exception:
                break
            if chunk == HDF5_MAGIC:
                out["format"] = "hdf5"
                out["hdf5_superblock_offset"] = off
                notes.append(f"HDF5 superblock found at offset {off} (after a user block)")
                return out

    if head[:1] in (b"{", b"["):
        blob = head
        if offset_reads is not None:
            try:
                blob += offset_reads(len(head), 4088)
            except Exception:
                pass
        if b'"refs"' in blob or (b'"version"' in blob and b'"templates"' in blob):
            out["format"] = "kerchunk"
            notes.append("Kerchunk/VirtualiZarr reference JSON (top-level refs key)")
        else:
            out["format"] = "unknown"
            notes.append("JSON but no recognized refs/templates keys; inspect manually")
        return out

    if head.startswith(b"PAR1"):
        out["format"] = "parquet"
        notes.append("possibly kerchunk parquet references or GeoParquet")
        return out

    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        out["format"] = "cog"
        notes.append("TIFF magic; COG-ness not verified from bytes alone")
        return out

    if head.startswith(b"GRIB"):
        out["format"] = "grib2"
        return out

    out["format"] = "unknown"
    notes.append(f"unrecognized magic bytes: {head[:8].hex()}")
    return out


# ---------------------------------------------------------- hdf5 refinement
def refine_hdf5(fs, path) -> dict:
    """Is this HDF5 file actually NetCDF-4? Soft-imports h5py; when
    h5py isn't installed, returns the unrefined result plus a
    "refinement" note rather than failing.
    """
    out = {"format": "hdf5", "confidence": "magic", "notes": []}
    try:
        import h5py
    except ImportError:
        out["refinement"] = "skipped: h5py not installed"
        return out

    try:
        with fs.open(path, "rb") as f, h5py.File(f, "r") as h:
            if "_NCProperties" in h.attrs:
                nc = h.attrs["_NCProperties"]
                out["format"] = "netcdf4"
                out["nc_properties"] = nc.decode() if isinstance(nc, bytes) else str(nc)
                out["refinement"] = "netcdf4 (_NCProperties attr)"
                return out
            # dimension scales are a strong netCDF-4 signal
            for _, obj in h.items():
                if hasattr(obj, "attrs") and "CLASS" in obj.attrs:
                    cls = obj.attrs["CLASS"]
                    cls = cls.decode() if isinstance(cls, bytes) else str(cls)
                    if cls == "DIMENSION_SCALE":
                        out["format"] = "netcdf4"
                        out["refinement"] = "netcdf4 (DIMENSION_SCALE, no _NCProperties)"
                        return out
            out["refinement"] = "hdf5 (no netcdf4 markers found)"
            return out
    except Exception as e:
        out["refinement"] = f"failed: {type(e).__name__}: {e}"
        return out


# -------------------------------------------------------------- store layout
def sniff_store(fs, root: str) -> dict:
    """Zarr v2/v3/Icechunk store-layout detection. Works against any
    fsspec-like fs exposing ``.exists(path)`` (and, optionally,
    ``.cat_file(path)`` to verify consolidated metadata in a Zarr v3
    zarr.json). Includes a consolidated-vs-dispersed metadata note.
    """
    base = root.rstrip("/")

    def exists(name):
        try:
            return fs.exists(f"{base}/{name}")
        except Exception:
            return False

    if exists("zarr.json"):
        out = {"format": "zarr", "zarr_version": "v3", "confidence": "store-layout"}
        consolidated = None
        cat_file = getattr(fs, "cat_file", None)
        if cat_file is not None:
            try:
                raw = cat_file(f"{base}/zarr.json")
                doc = json.loads(raw)
                consolidated = doc.get("consolidated_metadata") is not None
            except Exception:
                consolidated = None
        out["consolidated_metadata"] = consolidated
        if consolidated:
            out["notes"] = ["consolidated_metadata present in zarr.json"]
        else:
            out["notes"] = ["check zarr.json for a consolidated_metadata key "
                            "(could not verify from store listing alone)"]
        return out

    if exists(".zmetadata"):
        return {"format": "zarr", "zarr_version": "v2",
                "consolidated_metadata": True, "confidence": "store-layout",
                "notes": ["consolidated .zmetadata present"]}

    if exists(".zgroup") or exists(".zarray"):
        return {"format": "zarr", "zarr_version": "v2",
                "consolidated_metadata": False, "confidence": "store-layout",
                "notes": ["No .zmetadata - readers must crawl the store to "
                          "discover arrays (a metadata-dispersal finding)."]}

    if exists("refs") and (exists("snapshots") or exists("manifests")):
        return {"format": "icechunk", "confidence": "store-layout", "notes": []}

    return {"format": "unknown", "confidence": "store-layout",
            "notes": ["no recognized store layout markers found"]}


# ----------------------------------------------------------------- classing
CLOUD_NATIVE = {"cog", "zarr", "icechunk", "parquet", "copc", "flatgeobuf", "pmtiles"}
CLOUD_OPTIMIZABLE = {"hdf5", "netcdf4", "las", "kerchunk"}
CLOUD_HOSTILE = {"netcdf3", "grib2", "shapefile", "csv", "zip", "tar", "gzip", "hdf4"}


def format_class(fmt: str) -> str:
    """Classify a canonical format string as "cloud-native",
    "cloud-optimizable", "cloud-hostile", or "unknown".
    """
    if fmt in CLOUD_NATIVE:
        return "cloud-native"
    if fmt in CLOUD_OPTIMIZABLE:
        return "cloud-optimizable"
    if fmt in CLOUD_HOSTILE:
        return "cloud-hostile"
    return "unknown"
