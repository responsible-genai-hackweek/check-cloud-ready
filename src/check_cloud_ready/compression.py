"""Codec inspection (S3) + optional empirical benchmark grid (S9).

Ported from two sources (see the task brief for line references):

- ``.claude/skills/check-cloud-ready/scripts/compression_bench.py``:
  ``build_grid``/``run_config`` -- the conventional codec grid (zstd
  1/3/5 x shuffle/noshuffle, blosc-lz4 +/- shuffle, blosc-zstd-3 +/-
  shuffle, optional BitRound lossy row), with throughput numbers
  (ratio, compress/decompress MB/s, ``max_abs_error`` for bitround).
  Its ``load_sample_chunks`` is *not* ported -- Task 7's
  ``chunking.pick_interior_chunks``/``chunking.is_data_bearing`` already
  cover S4-compliant sample selection, and sample selection is the
  caller's job here anyway (see ``benchmark``'s docstring).
- ``.claude/skills/earth-science-cloud-readiness/scripts/smoke_test.py``
  ``_trial_codecs`` (a simpler version of the same grid, no bitround) and
  ``.../scripts/assess.py``'s C5-codec judgment (the ``modern``/``weak``
  codec-string classification, and the "no compression observed" bug
  this module's ``inspect_codec`` fixes -- see S3 fix below).

S3 fix: the ported C5-codec judgment in assess.py read a single
top-level key (``lo.get("compression")``, i.e. ``lazy_open["compression"]``)
which only ever existed for HDF5's *first* visited dataset, not per
variable -- so a fully gzip-compressed file with many variables could be
reported as "no compression observed" if that one lucky key was absent
or the wrong shape. ``inspect_codec`` here reads the per-variable
``codec`` field (Ruling I-1; populated by ``openers.py`` -- HDF5
per-dataset ``ds.compression``+shuffle, zarr's codec pipeline) off
*every* inventory record, independently.

Ruling I-3: like ``chunking.py``, this module does no I/O and never
constructs a filesystem. ``inspect_codec`` operates purely on inventory
records (dicts) the caller already has; ``benchmark``/``assess_compression``
operate purely on an already-decoded in-memory numpy sample the caller
already obtained (e.g. via ``chunking.pick_interior_chunks`` +
``chunking.is_data_bearing`` and a decode of that chunk) -- there is no
``fs`` or ``budget`` parameter anywhere in this module's public API.
"""
from __future__ import annotations

import re
import time
from typing import Any

import numpy as np

__all__ = ["inspect_codec", "benchmark", "assess_compression"]

# --------------------------------------------------------------- soft imports


def _try_import(name):
    try:
        return __import__(name)
    except Exception:
        return None


numcodecs = _try_import("numcodecs")

# ------------------------------------------------------------- S3: inspection

# Ported judgment (assess.py's C5-codec, collapsed to this module's simpler
# pass/warn/fail per-variable shape -- see the module docstring's S3 fix):
#   gzip/zlib/deflate -> warn (zstd dominates it on every axis)
#   zstd/blosc        -> pass
#   falsy/"none"/...  -> fail (uncompressed)
#   anything else     -> warn, unrecognized (can't judge automatically)
_UNCOMPRESSED_STRINGS = {"none", "uncompressed", "raw", "", "no compression"}
_WEAK_CODEC_RE = re.compile(r"gzip|zlib|deflate", re.IGNORECASE)
_MODERN_CODEC_RE = re.compile(r"zstd|blosc", re.IGNORECASE)

_GZIP_REMEDIATION = "recompress with zstd+shuffle (faster decode at similar ratio)"
_UNCOMPRESSED_REMEDIATION = ("enable compression -- zstd level 3 + shuffle is a strong, "
                             "defensible default")


def _judge_codec(codec: str | None) -> tuple[str, str, str | None]:
    """Returns ``(status, note, remediation)`` for one codec string."""
    if codec is None or str(codec).strip().lower() in _UNCOMPRESSED_STRINGS:
        return ("fail", "no compression detected for this variable",
                _UNCOMPRESSED_REMEDIATION)
    if _WEAK_CODEC_RE.search(codec):
        return ("warn",
                f"{codec}: works, but zstd/blosc dominate gzip/zlib/deflate on every axis",
                _GZIP_REMEDIATION)
    if _MODERN_CODEC_RE.search(codec):
        return ("pass", f"{codec}: modern read-favoring codec", None)
    return ("warn", f"unrecognized codec string {codec!r}; cannot judge automatically "
                    "-- review manually", None)


def inspect_codec(variables: list[dict]) -> dict:
    """Per-variable codec inspection. Reads the ``codec`` field off EACH
    inventory record independently (never a single top-level/shared
    key -- see the module docstring's S3 fix). Always runs; no soft
    dependency, no I/O.

    Returns a list of ``{"name", "codec", "status": "pass"|"warn"|"fail",
    "note", "remediation"}`` dicts, one per input record.
    """
    out = []
    for rec in variables:
        codec = rec.get("codec")
        status, note, remediation = _judge_codec(codec)
        out.append({
            "name": rec.get("name"),
            "codec": codec,
            "status": status,
            "note": note,
            "remediation": remediation,
        })
    return out


# ------------------------------------------------------------- S9: benchmark

_MIN_BYTES = 64 * 1024  # ported guard: sample too small to be meaningful

# Standard grid, ported verbatim from compression_bench.py's build_grid
# (config-name, (cname_or_"zstd", level)) -- Shuffle is applied separately
# per-row below since it needs the sample's itemsize.
_ZSTD_LEVELS = (1, 3, 5)
_BLOSC_VARIANTS = (("lz4", 5), ("zstd", 3))


def _reproduce_current_codec(current_codec: str | None, itemsize: int):
    """Map a codec string to a list of numcodecs codec instances that
    reproduce it (encode order; decode is the reverse). Returns
    ``(codecs, label)`` where ``codecs`` is ``None`` if the codec string
    is exotic/unrecognized (caller emits a placeholder row instead), or
    ``label`` describing what was reproduced (e.g. "uncompressed").

    ``None``/uncompressed-string current_codec reproduces as an empty
    codecs list (the stored/no-op baseline), which is a *supported*
    outcome, not "exotic" -- distinct from an unrecognized non-empty
    string like "lzf".
    """
    from numcodecs import Blosc, Shuffle, Zlib, Zstd

    if current_codec is None or str(current_codec).strip().lower() in _UNCOMPRESSED_STRINGS:
        return [], "uncompressed"

    s = current_codec
    sl = s.lower()
    want_shuffle = "shuffle" in sl and "noshuffle" not in sl

    m = re.search(r"blosc-(lz4|zstd)(?:[-(](\d+)\)?)?", sl)
    if m:
        cname = m.group(1)
        level = int(m.group(2)) if m.group(2) else (5 if cname == "lz4" else 3)
        shuffle_mode = Blosc.SHUFFLE if want_shuffle else Blosc.NOSHUFFLE
        return [Blosc(cname=cname, clevel=level, shuffle=shuffle_mode)], s

    m = re.search(r"zstd(?:[-(](\d+)\)?)?", sl)
    if m and "blosc" not in sl:
        level = int(m.group(1)) if m.group(1) else 3
        codecs = [Shuffle(itemsize)] if want_shuffle else []
        codecs.append(Zstd(level=level))
        return codecs, s

    m = re.search(r"gzip(?:[-(](\d+)\)?)?", sl)
    if m:
        level = int(m.group(1)) if m.group(1) else 4  # "level from file if known else 4"
        codecs = [Shuffle(itemsize)] if want_shuffle else []
        codecs.append(Zlib(level=level))
        return codecs, s

    return None, s  # exotic: cannot reproduce locally


def _build_grid(itemsize: int):
    """Standard lossless grid: {zstd-1,3,5, blosc-lz4, blosc-zstd-3} x
    {shuffle, noshuffle}. Returns [(config_name, [codec, ...]), ...].
    """
    from numcodecs import Blosc, Shuffle, Zstd

    grid = []
    for lvl in _ZSTD_LEVELS:
        grid.append((f"zstd-{lvl}+shuffle", [Shuffle(itemsize), Zstd(level=lvl)]))
        grid.append((f"zstd-{lvl}", [Zstd(level=lvl)]))
    for cname, lvl in _BLOSC_VARIANTS:
        grid.append((f"blosc-{cname}-{lvl}+shuffle",
                     [Blosc(cname=cname, clevel=lvl, shuffle=Blosc.SHUFFLE)]))
        grid.append((f"blosc-{cname}-{lvl}",
                     [Blosc(cname=cname, clevel=lvl, shuffle=Blosc.NOSHUFFLE)]))
    return grid


def _run_codecs(codecs, raw: bytes) -> dict:
    """Encode/decode ``raw`` through ``codecs`` (applied in order for
    encode, reversed for decode); returns ratio + throughput. Ported
    from compression_bench.py's run_config, simplified to a single
    buffer (the caller already has one decoded sample, not multiple
    sampled chunks)."""
    buf: Any = raw
    t0 = time.perf_counter()
    for c in codecs:
        buf = c.encode(buf)
    t_comp = time.perf_counter() - t0
    comp_bytes = buf if isinstance(buf, (bytes, bytearray)) else bytes(buf)

    t0 = time.perf_counter()
    out = comp_bytes
    for c in reversed(codecs):
        out = c.decode(out)
    t_decomp = time.perf_counter() - t0

    mb = len(raw) / 2**20
    return {
        "ratio": round(len(raw) / len(comp_bytes), 3) if comp_bytes else None,
        "compress_MBps": round(mb / t_comp, 1) if t_comp > 0 else None,
        "decompress_MBps": round(mb / t_decomp, 1) if t_decomp > 0 else None,
    }, out


def benchmark(data: "np.ndarray", current_codec: str | None, *,
              keepbits: int | None = None) -> list[dict]:
    """Empirical compression grid on one already-decoded sample.
    Sample selection is the CALLER's job -- pass a decoded interior
    data-bearing chunk (see ``chunking.pick_interior_chunks`` +
    ``chunking.is_data_bearing``). No I/O happens here.

    Row one reproduces the dataset's current codec (best-effort; see
    ``_reproduce_current_codec``). If it can't be reproduced locally
    (exotic codec string), row one is a placeholder
    ``{"config": "current (<name>)", "error": "cannot reproduce
    locally", "is_current": True}`` and the standard grid still runs
    after it.

    Then the standard lossless grid: {zstd-1,3,5, blosc-lz4,
    blosc-zstd-3} x {shuffle, noshuffle}. Each row:
    ``{"config", "ratio", "compress_MBps", "decompress_MBps",
    "is_current": bool}``.

    ``keepbits`` (optional): also runs a lossy BitRound variant of every
    grid row (prefixed ``bitround<keepbits>+``), each with
    ``"lossy": True`` and a measured ``max_abs_error`` against the
    original array. Lossy rows are a provider decision -- never merged
    into lossless comparisons by this function (callers must filter on
    ``"lossy"``).

    Returns ``[]`` if the sample is <64 KB (ported guard: too small to
    be a meaningful compression trial), or a single
    ``[{"status": "skipped", "reason": "numcodecs not installed"}]`` row
    if numcodecs is unavailable.
    """
    if numcodecs is None:
        return [{"status": "skipped", "reason": "numcodecs not installed"}]

    arr = np.ascontiguousarray(data)
    if arr.nbytes < _MIN_BYTES:
        return []

    raw = arr.tobytes()
    itemsize = arr.itemsize
    rows: list[dict] = []

    # Row one: reproduce the dataset's current codec.
    codecs, label = _reproduce_current_codec(current_codec, itemsize)
    config_name = f"current ({label})"
    if codecs is None:
        rows.append({"config": config_name, "error": "cannot reproduce locally",
                     "is_current": True})
    elif not codecs:  # uncompressed baseline
        rows.append({"config": config_name, "ratio": 1.0, "compress_MBps": None,
                     "decompress_MBps": None, "is_current": True})
    else:
        try:
            stats, _ = _run_codecs(codecs, raw)
            rows.append({"config": config_name, "is_current": True, **stats})
        except Exception as e:
            rows.append({"config": config_name,
                         "error": f"reproduction failed: {type(e).__name__}: {e}",
                         "is_current": True})

    # Standard grid.
    grid = _build_grid(itemsize)
    for name, grid_codecs in grid:
        try:
            stats, _ = _run_codecs(grid_codecs, raw)
            rows.append({"config": name, "is_current": False, **stats})
        except Exception as e:
            rows.append({"config": name, "is_current": False,
                         "error": f"{type(e).__name__}: {e}"})

    # Optional lossy BitRound variants of the same grid.
    if keepbits is not None:
        try:
            from numcodecs import BitRound
        except ImportError:
            BitRound = None
        if BitRound is not None:
            for name, grid_codecs in grid:
                lossy_codecs = [BitRound(keepbits=keepbits)] + grid_codecs
                lossy_name = f"bitround{keepbits}+{name}"
                try:
                    stats, decoded_bytes = _run_codecs(lossy_codecs, raw)
                    decoded = np.frombuffer(decoded_bytes, dtype=arr.dtype).reshape(arr.shape)
                    max_abs_error = float(np.nanmax(np.abs(
                        decoded.astype("f8") - arr.astype("f8"))))
                    rows.append({"config": lossy_name, "is_current": False, "lossy": True,
                                "max_abs_error": max_abs_error, **stats})
                except Exception as e:
                    rows.append({"config": lossy_name, "is_current": False, "lossy": True,
                                "error": f"{type(e).__name__}: {e}"})
        else:
            rows.append({"config": f"bitround{keepbits}", "is_current": False, "lossy": True,
                        "error": "BitRound unavailable in installed numcodecs"})

    return rows


# ------------------------------------------------------- assess_compression

def assess_compression(variables: list[dict], *, sample: "np.ndarray | None",
                       run_benchmark: bool, keepbits: int | None = None) -> dict:
    """Combine S3 codec inspection (always) with the S9 empirical
    benchmark grid (only when ``run_benchmark`` and ``sample`` is
    given). No I/O of its own -- ``sample`` is an already-decoded numpy
    array the caller obtained (per Ruling I-3).

    The current codec fed to ``benchmark`` is taken from the first
    record in ``variables`` (the primary variable the caller sampled
    ``sample`` from) -- ``variables`` is expected to describe that one
    variable, or have it first, since there is no separate
    variable-name parameter tying ``sample`` to a specific record.

    Returns ``{"inspection": [...], "benchmark": [...] | None,
    "notes": [...]}``.
    """
    inspection = inspect_codec(variables)
    notes: list[str] = []
    bench = None

    if run_benchmark and sample is not None:
        current_codec = variables[0].get("codec") if variables else None
        bench = benchmark(sample, current_codec, keepbits=keepbits)
        if not bench and np.ascontiguousarray(sample).nbytes < _MIN_BYTES:
            notes.append(f"sample <64 KB ({np.ascontiguousarray(sample).nbytes} bytes) -- "
                        "too small for a meaningful compression benchmark (skipped)")

    return {"inspection": inspection, "benchmark": bench, "notes": notes}
